#!/usr/bin/env python3

import itertools
import os
import sys
import time
import re
import json

import attr
import backoff
import pendulum
import requests
import dateutil.parser
import singer
import singer.metrics as metrics
from singer import utils


class SourceUnavailableException(Exception):
    pass


REQUIRED_CONFIG_KEYS = ["api_key", "user_agent"]
PER_PAGE = 100
BASE_URL = "https://api2.autopilothq.com/v1"


LOGGER = singer.get_logger()
SESSION = requests.session()

CONFIG = {}
STATE = {}

ENDPOINTS = {
    "contacts":                "/contacts",
    "lists_contacts":          "/list/{list_id}/contacts",
    "lists":                   "/lists",
    "smart_segments":          "/smart_segments",
    "smart_segments_contacts": "/smart_segments/{segment_id}/contacts",
}


def get_abs_path(path):
    '''Returns the absolute path'''
    return os.path.join(os.path.dirname(os.path.realpath(__file__)), path)


def load_schema(entity):
    '''Returns the schema for the specified source'''
    return utils.load_json(get_abs_path("schemas/{}.json".format(entity)))


def client_error(exc):
    '''Indicates whether the given RequestException is a 4xx response'''
    return exc.response is not None and 400 <= exc.response.status_code < 500


def parse_source_from_url(url):
    '''Given an Autopilot URL, extract the source name (e.g. "contacts")'''
    url_regex = re.compile(BASE_URL +  r'.*/(\w+)')
    match = url_regex.match(url)

    if match:
        if match.group(1) == "contacts":
            if "/list/" in match.group(0):
                return "lists_contacts"
            elif "segment" in match.group(0):
                return "smart_segments_contacts"
        return match.group(1)

    raise ValueError("Can't determine stream from URL " + url)

def parse_key_from_source(source):
    '''Given an Autopilot source, return the key needed to access the children
       The endpoints for fetching contacts related to a list or segment
       have the contacts in a child with the key of contacts
    '''
    if 'contact' in source:
        return 'contacts'

    elif 'smart_segments' in source:
        return 'segments'

    return source


def get_start(key):
    '''Get the start date from CONFIG or STATE

    TODO: This needs to grab the updated_at for state
    '''
    if key not in STATE:
        STATE[key] = CONFIG["start_date"]

    return STATE[key]


def get_current_stream(state):
    '''Retrieve a current stream from STATE if it exists'''
    if "current" in state:
        return state["current"]

    return None


def get_bookmark(key):
    '''Retrieve a bookmark from STATE if it exists'''
    if key in STATE:
        return "/" + STATE[key]

    return ""


def get_url(endpoint, **kwargs):
    '''Get the full url for the endpoint'''
    if endpoint not in ENDPOINTS:
        raise ValueError("Invalid endpoint {}".format(endpoint))

    return BASE_URL + ENDPOINTS[endpoint].format(**kwargs)


@backoff.on_exception(backoff.expo,
                      (requests.exceptions.RequestException),
                      max_tries=5,
                      giveup=client_error,
                      factor=2)
def request(url, params=None):
    '''Make a request to the given Autopilot URL.
    Handles retrying, status checking. Logs request duration and records
    per second
    '''
    headers = {"autopilotapikey": CONFIG["api_key"]}
    if "user_agent" in CONFIG:
        headers["user-agent"] = CONFIG["user_agent"]

    if "bookmark" in params:
        url = url + "/" + params["bookmark"]

    req = requests.Request("GET", url, headers=headers).prepare()
    LOGGER.info("GET %s", req.url)

    with metrics.http_request_timer(parse_source_from_url(url)) as timer:
        resp = SESSION.send(req)
        timer.tags[metrics.Tag.http_status_code] = resp.status_code
        resp.raise_for_status()
        return resp


def gen_request(endpoint, params=None):
    '''Generate a request that will iterate through the results
    and paginate through the responses until the amount of results
    returned is less than 100, the amount returned by the API.

    The API only returns bookmarks for iterating through contacts
    '''
    params = params or {}

    source = parse_source_from_url(endpoint)
    source_key = parse_key_from_source(source)

    with metrics.record_counter(source) as counter:
        while True:
            data = request(endpoint, params).json()
            if 'contact' in source:
                if "bookmark" in data:
                    params["bookmark"] = data["bookmark"]
                    utils.update_state(STATE, source, data["bookmark"])
                    singer.write_state(STATE)
                else:
                    params = {}
                    utils.update_state(STATE, source, None)
                    singer.write_state(STATE)

            for row in data[source_key]:
                counter.increment()
                yield row

            if len(data[source_key]) < PER_PAGE:
                params = {}
                break


def sync_contacts(STATE, catalog):
    '''Sync contacts from the Autopilot API

    The API returns data in the following format

    {
        "contacts": [{...},{...}],
        "total_contacts": 400,
        "bookmark": "person_9EAF39E4-9AEC-4134-964A-D9D8D54162E7"
    }

    TODO: Handle custom properties so that there aren't
    unlimited columns in a database

    '''
    schema = load_schema("contacts")
    singer.write_schema("contacts", schema, ["contact_id"], catalog.get("stream_alias"))

    bookmark = get_bookmark("contacts")
    params = {bookmark: bookmark}

    for row in gen_request(get_url("contacts"), params):
        singer.write_record("contacts", row)
        utils.update_state(STATE, "contacts", row["contact_id"])

    singer.write_state(STATE)

    LOGGER.info("Completed Contacts Sync")
    return STATE


def sync_lists(STATE, catalog):
    '''Sync all lists from the Autopilot API

    The API returns data in the following format

    {
        "lists": [
            {
            "list_id": "contactlist_06444749-9C0F-4894-9A23-D6872F9B6EF8",
            "title": "1k.csv"
            },
            {
            "list_id": "contactlist_0FBA1FA2-5A12-413B-B1A8-D113E6B3CDA8",
            "title": "____NEW____"
            }
        ]
     }

    '''
    schema = load_schema("lists")
    singer.write_schema("lists", schema, ["list_id"], catalog.get("stream_alias"))

    for row in gen_request(get_url("lists")):
        singer.write_record("lists", row)
        utils.update_state(STATE, "lists", row["list_id"])

    singer.write_state(STATE)
    LOGGER.info("Completed Lists Sync")
    return STATE


def sync_list_contacts(STATE, catalog):
    '''Sync the contacts on a given list from the Autopilot API'''
    schema = load_schema("lists_contacts")
    singer.write_schema(
        "lists_contacts",
        schema,
        ["list_id", "contact_id"],
        catalog.get("stream_alias"))

    params = {}

    for row in gen_request(get_url("lists"), params):
        subrow_url = get_url("lists_contacts", list_id=row["list_id"])
        for subrow in gen_request(subrow_url, params):
            singer.write_record("lists_contacts", {
                "list_id": row["list_id"],
                "contact_id": subrow["contact_id"],
            })

        utils.update_state(STATE, "lists_contacts", row["list_id"])
        LOGGER.info("Completed List's Contacts Sync")

    singer.write_state(STATE)
    LOGGER.info("Completed List Contacts Sync")
    return STATE


def sync_smart_segments(STATE, catalog):
    '''Sync all smart segments from the Autopilot API

    The API returns data in the following format

    {
        "segments": [
            {
            "segment_id": "contactlist_sseg1456891025207",
            "title": "Ladies"
            },
            {
            "segment_id": "contactlist_sseg1457059448884",
            "title": "Gentlemen"
            }
        ]
    }

    '''
    schema = load_schema("smart_segments")
    singer.write_schema("smart_segments", schema, ["segment_id"], catalog.get("stream_alias"))
    params = {}

    for row in gen_request(get_url("smart_segments"), params):
        singer.write_record("smart_segments", row)
        utils.update_state(STATE, "smart_segments", row["segment_id"])

    singer.write_state(STATE)
    LOGGER.info("Completed Smart Segments Sync")
    return STATE


def sync_smart_segment_contacts(STATE, catalog):
    '''Sync the contacts on a given smart segment from the Autopilot API

    {
        "contacts": [{...},{...}],
        "total_contacts": 2
    }
    '''
    schema = load_schema("smart_segments_contacts")
    singer.write_schema("smart_segments_contacts", schema, ["segment_id", "contact_id"], catalog.get("stream_alias"))
    params = {}

    for row in gen_request(get_url("smart_segments"), params):
        subrow_url = get_url("smart_segments_contacts", segment_id=row["segment_id"])
        for subrow in gen_request(subrow_url, params):
            singer.write_record("smart_segment_id", {
                "segment_id": row["segment_id"],
                "contact_id": subrow["contact_id"]
            })

        utils.update_state(STATE, "smart_segments_contacts", row["segment_id"])
        LOGGER.info("Completed Smart Segment's Contacts Sync")


    singer.write_state(STATE)
    LOGGER.info("Completed Smart Segments Contacts Sync")
    return STATE


@attr.s
class Stream(object):
    tap_stream_id = attr.ib()
    sync = attr.ib()

STREAMS = [
    Stream("contacts", sync_contacts),
    Stream("lists", sync_lists),
    Stream("lists_contacts", sync_list_contacts),
    Stream("smart_segments", sync_smart_segments),
    Stream("smart_segments_contacts", sync_smart_segment_contacts)
]


def get_streams_to_sync(streams, state):
    '''Get the streams to sync'''
    current_stream = get_current_stream(state)
    result = streams
    if current_stream:
        result = list(itertools.dropwhile(
            lambda x: x.tap_stream_id != current_stream, streams))
    if not result:
        raise Exception("Unknown stream {} in state".format(current_stream))
    return result

def get_selected_streams(remaining_streams, annotated_schema):
    selected_streams = []


    for stream in remaining_streams:
        tap_stream_id = stream.tap_stream_id
        for annotated_stream in annotated_schema["streams"]:
            if tap_stream_id == annotated_stream["tap_stream_id"]:
                schema = annotated_stream["schema"]
                LOGGER.info(schema)
                if "selected" in schema and schema["selected"] == True:
                    selected_streams.append(stream)

    return selected_streams


def do_sync(STATE, catalogs):
    '''Do a full sync'''
    remaining_streams = get_streams_to_sync(STREAMS, STATE)
    selected_streams = get_selected_streams(remaining_streams, catalogs)
    LOGGER.info("Starting sync. Will sync these streams: %s",
                [stream.tap_stream_id for stream in selected_streams])

    for stream in selected_streams:
        LOGGER.info("Syncing %s", stream.tap_stream_id)
        utils.update_state(STATE, "current", stream.tap_stream_id)
        singer.write_state(STATE)

        try:
            catalog = [c for c in catalogs.get('streams')
                       if c.get('stream') == stream.tap_stream_id][0]
            STATE = stream.sync(STATE, catalog)
        except SourceUnavailableException:
            pass

    utils.update_state(STATE, "current", None)
    singer.write_state(STATE)
    LOGGER.info("Sync completed")

def load_discovered_schema(stream):
    schema = load_schema(stream.tap_stream_id)
    for k in schema['properties']:
        schema['properties'][k]['inclusion'] = 'automatic'
    return schema


def discover_schemas():
    result = {'streams': []}
    for stream in STREAMS:
        LOGGER.info('Loading schema for %s', stream.tap_stream_id)
        result['streams'].append({'stream': stream.tap_stream_id,
                                  'tap_stream_id': stream.tap_stream_id,
                                  'schema': load_discovered_schema(stream)})
    return result


def do_discover():
    LOGGER.info("Loading Schemas")
    json.dump(discover_schemas(), sys.stdout, indent=4)

def main():
    '''Entry point'''
    args = utils.parse_args(REQUIRED_CONFIG_KEYS)

    CONFIG.update(args.config)

    if args.state:
        STATE.update(args.state)

    if args.discover:
        do_discover()
    elif args.catalog:
        do_sync(STATE, args.catalog.to_dict())
    else:
        LOGGER.info("No Streams were selected")


if __name__ == "__main__":
    main()