"""Event filters and keyset cursors shared by GET /events and GET /reports/{id}/events.

Field filter syntax (deliberately tiny, not a query language):

    field=<key>:<value>      repeatable; all filters must match (AND)

    field=path:ARRIVAL       -> TRIGGER_ALLOWED lines with path=ARRIVAL
    field=risk:HIGH&field=reason:GPS_ONLY_NO_DATA_NO_WIFI

- key must look like a log key: [A-Za-z_][A-Za-z0-9_.]* (so it cannot smuggle JSON or SQL);
- value is everything after the first ':' and is compared as an exact string, because every
  value is stored as the string the app logged ("8.732", not 8.732);
- each filter becomes a JSONB containment test, which the GIN index on events.fields serves.

Duplicate keys are stored as lists ("tier": ["blind_motion", "blind_motion"]), and Postgres
containment does NOT treat {"tier": [..]} as containing {"tier": "blind_motion"}. So each filter
matches either shape:  fields @> {"k": "v"}  OR  fields @> {"k": ["v"]}.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import ColumnElement, and_, or_

from ..models import Event

_FIELD_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")


class InvalidQuery(ValueError):
    pass


@dataclass(frozen=True)
class FieldFilter:
    key: str
    value: str


def parse_field_filters(raw: list[str] | None) -> list[FieldFilter]:
    filters = []
    for item in raw or []:
        key, sep, value = item.partition(":")
        if not sep:
            raise InvalidQuery(f"field filter {item!r} must look like key:value")
        if not _FIELD_KEY.match(key):
            raise InvalidQuery(f"field filter key {key!r} is not a valid log key")
        filters.append(FieldFilter(key, value))
    return filters


def field_filter_clause(f: FieldFilter) -> ColumnElement[bool]:
    # .contains() on a JSONB column renders "@>", with the dict bound as a JSONB parameter.
    return or_(
        Event.fields.contains({f.key: f.value}),
        Event.fields.contains({f.key: [f.value]}),
    )


def event_filter_clauses(
    *,
    tags: list[str] | None,
    subtag: str | None,
    build_sha: str | None,
    alarm_id: str | None,
    ts_from: datetime | None,
    ts_to: datetime | None,
    fields: list[FieldFilter],
) -> list[ColumnElement[bool]]:
    clauses: list[ColumnElement[bool]] = []
    if tags:
        clauses.append(Event.tag.in_(tags))
    if subtag is not None:
        clauses.append(Event.subtag == subtag)
    if build_sha is not None:
        clauses.append(Event.build_sha == build_sha)
    if alarm_id is not None:
        clauses.append(Event.alarm_id == alarm_id)
    if ts_from is not None:
        clauses.append(Event.device_ts >= ts_from)
    if ts_to is not None:
        clauses.append(Event.device_ts < ts_to)
    clauses.extend(field_filter_clause(f) for f in fields)
    return clauses


# --- cursors ---------------------------------------------------------------------------------
# A cursor is the sort key of the last row on the previous page, base64-encoded so clients
# treat it as opaque. The next page is "rows strictly after that key", which stays correct and
# fast however deep you page, unlike OFFSET which rescans every skipped row.


def encode_cursor(payload: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()


def decode_cursor(cursor: str) -> dict:
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode()))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise InvalidQuery("malformed cursor") from None
    if not isinstance(payload, dict):
        raise InvalidQuery("malformed cursor")
    return payload


def global_cursor_clause(cursor: str) -> ColumnElement[bool]:
    """GET /events order: (device_ts, id). Ties on device_ts are broken by id."""
    payload = decode_cursor(cursor)
    try:
        ts = datetime.fromisoformat(payload["ts"])
        last_id = int(payload["id"])
    except (KeyError, TypeError, ValueError):
        raise InvalidQuery("malformed cursor") from None
    return or_(Event.device_ts > ts, and_(Event.device_ts == ts, Event.id > last_id))


def global_cursor_for(event: Event) -> str:
    return encode_cursor({"ts": event.device_ts.isoformat(), "id": event.id})


def report_cursor_clause(cursor: str) -> ColumnElement[bool]:
    """GET /reports/{id}/events order: line_no, unique within a report."""
    payload = decode_cursor(cursor)
    try:
        line_no = int(payload["line"])
    except (KeyError, TypeError, ValueError):
        raise InvalidQuery("malformed cursor") from None
    return Event.line_no > line_no


def report_cursor_for(event: Event) -> str:
    return encode_cursor({"line": event.line_no})
