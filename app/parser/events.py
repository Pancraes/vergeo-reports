"""Grammar of one diagnostic event line.

A Vergeo DurableLog line looks like:

    2026-11-01 13:40:13.132|er=275099884|POSITIONING_HEALTH|at=JS_QUERY|risk=HIGH|...
    <device-local timestamp>|er=<elapsedRealtime ms>|<TAG>|<tokens...>

Tokens are split on "|". A token containing "=" is key=value, split on the FIRST "=" so a value
may itself contain "=". A token without "=" is a bare token:

- directly after the tag it is the subtag (2026 reports: "DIAG2|FIX|t=...|prov=fused");
- anywhere else it is kept, in order, under the reserved key "_bare". Real case: AlarmAudioService
  writes "AUDIO_STOP|source=" + source, and source itself can be "STOP_ALL|LOCKSCREEN_HOLD", so
  the log line ends "...|source=STOP_ALL|LOCKSCREEN_HOLD". The grammar cannot tell that the pipe
  belonged to the value, so the parser keeps the token rather than dropping it.

A key that repeats (BLIND_HOP_CANDIDATE writes "tier=" twice) becomes a list of its values in
order. A dict would silently keep only the last one.

This module knows nothing about reports, FastAPI or the database.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from .errors import MalformedEventLine

TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S.%f"
BARE_KEY = "_bare"

# "Intended to be an event" = the line starts with a year. Everything else (APP|, ENV|, "====
# ..." markers) is intentionally not an event and the report parser deals with it.
_EVENT_LOOKING = re.compile(r"^\d{4}-")
# Every one of the 125 tags in the historical reports matches this.
_TAG = re.compile(r"^[A-Z][A-Z0-9_]*$")

FieldValue = str | list[str]


@dataclass(frozen=True)
class ParsedEvent:
    line_no: int
    device_ts: datetime  # device-local wall clock, no timezone in the log
    er: int | None  # elapsedRealtime ms; None only for synthetic marker events
    tag: str
    subtag: str | None
    fields: dict[str, FieldValue]


def looks_like_event(line: str) -> bool:
    return bool(_EVENT_LOOKING.match(line))


def parse_event_line(line: str, line_no: int) -> ParsedEvent | None:
    """Parse one line.

    Returns None for a line that is intentionally not an event. Raises MalformedEventLine for a
    line that starts like an event but whose timestamp, er or tag is invalid.
    """
    if not looks_like_event(line):
        return None

    tokens = line.split("|")
    if len(tokens) < 3:
        raise MalformedEventLine(line_no, "expected '<timestamp>|er=<n>|<TAG>'")

    ts_text, er_text, tag = tokens[0], tokens[1], tokens[2]

    try:
        device_ts = datetime.strptime(ts_text, TIMESTAMP_FORMAT)
    except ValueError:
        raise MalformedEventLine(line_no, f"invalid timestamp {ts_text[:40]!r}") from None

    if not er_text.startswith("er="):
        raise MalformedEventLine(line_no, f"expected er=<n>, got {er_text[:40]!r}")
    try:
        er = int(er_text[3:])
    except ValueError:
        raise MalformedEventLine(line_no, f"invalid er value {er_text[:40]!r}") from None

    if not _TAG.match(tag):
        raise MalformedEventLine(line_no, f"invalid or missing tag {tag[:40]!r}")

    rest = tokens[3:]
    subtag = None
    if rest and rest[0] and "=" not in rest[0]:
        subtag = rest[0]
        rest = rest[1:]

    return ParsedEvent(
        line_no=line_no,
        device_ts=device_ts,
        er=er,
        tag=tag,
        subtag=subtag,
        fields=parse_fields(rest),
    )


def parse_fields(tokens: list[str]) -> dict[str, FieldValue]:
    """key=value tokens into a dict, keeping empty values, '=' inside values and repeated keys."""
    fields: dict[str, FieldValue] = {}
    for token in tokens:
        if token == "":
            continue  # "a||b": an empty token carries no information
        key, sep, value = token.partition("=")
        if not sep or key == "":
            key, value = BARE_KEY, token
        _add(fields, key, value)
    return fields


def _add(fields: dict[str, FieldValue], key: str, value: str) -> None:
    if key == BARE_KEY:
        existing = fields.setdefault(BARE_KEY, [])
        assert isinstance(existing, list)
        existing.append(value)
        return
    if key not in fields:
        fields[key] = value
        return
    existing = fields[key]
    if isinstance(existing, list):
        existing.append(value)
    else:
        fields[key] = [existing, value]
