"""Document structure of a Vergeo problem report.

Mirrors ProblemReport.assemble() in the Vergeo Android app:

    ==== VERGEO PROBLEM REPORT ====                          line 1, required
    APP|ver=1.9.0|code=44|debug=false|device=...|api=29      line 2, required
    ---- what the user reported ----                         line 3, required
    <description, may span several lines>
    ENV|fine=true|...                                        only when diagnostics are included
    ---- recent activity (sanitized, oldest first) ----      required
    <diagnostic lines, oldest first>
    ==== REPORT COMPLETE | diagnostics=true | lines=8514 | window_truncated=false ====

If the user opted out of diagnostics, the diagnostic section holds a single OMITTED_NOTICE line.

Inside the diagnostic section:

- event lines go to events.parse_event_line;
- "==== VERGEO SESSION START <ts> ====" (a process start) becomes a synthetic SESSION_START event,
  and the APP|/ENV| lines DurableLog writes right after it become that event's fields;
- anything malformed is recorded as a parse warning and skipped.

Two pieces of context need more than one line, so they live here and not in events.py:

- build_sha. The report header has no git SHA. It appears only as gitSha= on TEST_SESSION_START
  events, and one report can span several builds (the 2026-11-06 report contains three). Every
  event after a TEST_SESSION_START inherits its gitSha until the next VERGEO SESSION START, which
  clears it: a process restart may be a reinstall of a different build, so "unknown" is more
  honest than carrying the old SHA forward.
- alarm_id. The app writes the alarm's opaque UUID as ID= on most tags and alarm= on
  TEST_SESSION_START. Both spellings are promoted to one indexed column.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from .errors import MalformedEventLine, ReportFormatError
from .events import TIMESTAMP_FORMAT, ParsedEvent, looks_like_event, parse_event_line, parse_fields

# Bump when parsing output changes, so stored reports can be found and reprocessed.
PARSER_VERSION = 1

HEADER = "==== VERGEO PROBLEM REPORT ===="
DESC_HEADER = "---- what the user reported ----"
DIAG_HEADER = "---- recent activity (sanitized, oldest first) ----"
OMITTED_NOTICE = "(diagnostic information was not included in this report)"
TRAILER_PREFIX = "==== REPORT COMPLETE"
SESSION_START_PREFIX = "==== VERGEO SESSION START "

SESSION_START_TAG = "SESSION_START"
TEST_SESSION_START_TAG = "TEST_SESSION_START"

# A report with thousands of garbage lines should not produce a multi-megabyte warnings column.
MAX_STORED_WARNINGS = 200

_TRAILER = re.compile(
    r"^==== REPORT COMPLETE \| diagnostics=(true|false) \| lines=(\d+)"
    r" \| window_truncated=(true|false) ====$"
)
_SESSION_START = re.compile(r"^==== VERGEO SESSION START (.+) ====$")
_GIT_SHA = re.compile(r"^[0-9a-f]{7,40}$")


@dataclass(frozen=True)
class AppInfo:
    version: str
    version_code: int
    debug: bool | None
    device_model: str
    android_api: int


@dataclass(frozen=True)
class Trailer:
    diagnostics: bool
    lines: int
    window_truncated: bool


@dataclass(frozen=True)
class IndexedEvent:
    """A parsed event plus the context that needs neighbouring lines to work out."""

    event: ParsedEvent
    build_sha: str | None
    alarm_id: str | None


@dataclass
class ParsedReport:
    app: AppInfo
    env: dict[str, str] | None
    description: str
    diagnostics_included: bool
    trailer: Trailer | None
    events: list[IndexedEvent] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)
    warning_count: int = 0
    parser_version: int = PARSER_VERSION

    @property
    def window_start(self) -> datetime | None:
        return min((e.event.device_ts for e in self.events), default=None)

    @property
    def window_end(self) -> datetime | None:
        return max((e.event.device_ts for e in self.events), default=None)

    def warn(self, line_no: int | None, reason: str) -> None:
        self.warning_count += 1
        if len(self.warnings) < MAX_STORED_WARNINGS:
            self.warnings.append({"line_no": line_no, "reason": reason})


def parse_report(raw: bytes) -> ParsedReport:
    """Parse a whole report. Raises ReportFormatError only for required-structure problems."""
    text = raw.decode("utf-8", errors="replace")
    # Line numbers are 1-based physical lines of the raw file, so raw + line_no reconstructs any
    # event exactly. splitlines() would also split on \x0b, \x1c etc.; the app only writes \n.
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    lines = [line.removesuffix("\r") for line in lines]

    def line(i: int) -> str:  # i is 1-based
        return lines[i - 1] if i <= len(lines) else ""

    if line(1) != HEADER:
        raise ReportFormatError(f"line 1 must be {HEADER!r}")
    app = parse_app_line(line(2))
    if line(3) != DESC_HEADER:
        raise ReportFormatError(f"line 3 must be {DESC_HEADER!r}")

    try:
        diag_header_no = lines.index(DIAG_HEADER, 3) + 1
    except ValueError:
        raise ReportFormatError(f"missing {DIAG_HEADER!r}") from None

    # The description runs from line 4 to the line before ENV| (or before the diagnostics header
    # when ENV| is absent because the user opted out). It may contain newlines.
    desc_end = diag_header_no - 1
    env = None
    if desc_end >= 4 and line(desc_end).startswith("ENV|"):
        env = parse_env_line(line(desc_end))
        desc_end -= 1
    description = "\n".join(lines[3:desc_end])

    report = ParsedReport(
        app=app,
        env=env,
        description=description,
        diagnostics_included=True,
        trailer=None,
    )
    if "�" in text:
        report.warn(None, "report contained bytes that are not valid UTF-8; replaced with U+FFFD")

    _parse_diagnostics(lines, diag_header_no + 1, report)
    return report


def parse_app_line(line: str) -> AppInfo:
    if not line.startswith("APP|"):
        raise ReportFormatError("line 2 must be the APP| header")
    values = parse_fields(line.split("|")[1:])
    missing = [k for k in ("ver", "code", "device", "api") if not isinstance(values.get(k), str)]
    if missing:
        raise ReportFormatError(f"APP header missing {', '.join(missing)}")
    try:
        version_code = int(values["code"])
        android_api = int(values["api"])
    except ValueError:
        raise ReportFormatError("APP header code and api must be integers") from None
    debug = values.get("debug")
    return AppInfo(
        version=values["ver"],
        version_code=version_code,
        debug={"true": True, "false": False}.get(debug) if isinstance(debug, str) else None,
        device_model=values["device"],
        android_api=android_api,
    )


def parse_env_line(line: str) -> dict[str, str] | None:
    """ENV| holds permission/connectivity flags. "ENV|-" means the app had none to give."""
    body = line.removeprefix("ENV|")
    if body in ("", "-"):
        return None
    # ENV keys never repeat in practice; if one did, keep the last value rather than a list
    # because this dict is a flat snapshot.
    return {
        k: (v[-1] if isinstance(v, list) else v) for k, v in parse_fields(body.split("|")).items()
    }


def parse_trailer(line: str) -> Trailer | None:
    m = _TRAILER.match(line)
    if not m:
        return None
    return Trailer(
        diagnostics=m.group(1) == "true",
        lines=int(m.group(2)),
        window_truncated=m.group(3) == "true",
    )


def _parse_diagnostics(lines: list[str], first_no: int, report: ParsedReport) -> None:
    build_sha: str | None = None
    diagnostic_lines = 0
    # DurableLog writes each process start as a three-line block:
    #   ==== VERGEO SESSION START <ts> ====
    #   APP|ver=...|code=...|device=...|api=...
    #   ENV|fine=...|network=...|...
    # The APP/ENV lines are folded into that SESSION_START event's fields (their keys never
    # collide), so "which version/connectivity did this process start with" is queryable.
    open_session: ParsedEvent | None = None

    for line_no in range(first_no, len(lines) + 1):
        line = lines[line_no - 1]

        if open_session is not None and line.startswith(("APP|", "ENV|")):
            diagnostic_lines += 1
            open_session.fields.update(parse_fields(line.split("|")[1:]))
            continue
        open_session = None

        if line.startswith(TRAILER_PREFIX):
            report.trailer = parse_trailer(line)
            if report.trailer is None:
                report.warn(line_no, "unrecognised REPORT COMPLETE trailer")
            elif report.trailer.lines != diagnostic_lines:
                report.warn(
                    line_no,
                    f"trailer says lines={report.trailer.lines} but the diagnostic section "
                    f"has {diagnostic_lines}",
                )
            if line_no != len(lines):
                report.warn(line_no + 1, "content after the REPORT COMPLETE trailer ignored")
            return

        if line == OMITTED_NOTICE:
            report.diagnostics_included = False
            continue

        diagnostic_lines += 1
        if line == "":
            continue

        if line.startswith(SESSION_START_PREFIX):
            marker = _parse_session_start(line, line_no)
            if marker is None:
                report.warn(line_no, "unrecognised VERGEO SESSION START marker")
                continue
            build_sha = None  # new process: the running build is unknown again
            report.events.append(IndexedEvent(marker, build_sha=None, alarm_id=None))
            open_session = marker
            continue

        try:
            event = parse_event_line(line, line_no)
        except MalformedEventLine as e:
            report.warn(e.line_no, e.reason)
            continue
        if event is None:
            kind = "event-like" if looks_like_event(line) else "non-event"
            report.warn(line_no, f"unexpected {kind} line in diagnostic section")
            continue

        if event.tag == TEST_SESSION_START_TAG:
            sha = event.fields.get("gitSha")
            build_sha = sha if isinstance(sha, str) and _GIT_SHA.match(sha) else None

        report.events.append(
            IndexedEvent(event, build_sha=build_sha, alarm_id=_alarm_id(event.fields))
        )

    # Fell off the end without a trailer: the report was cut off (mail client, copy/paste, ...).
    # Keep everything parsed so far; trailer stays None and the API exposes trailer_present=false.
    report.warn(None, "no REPORT COMPLETE trailer: report is truncated")


def _parse_session_start(line: str, line_no: int) -> ParsedEvent | None:
    m = _SESSION_START.match(line)
    if not m:
        return None
    try:
        ts = datetime.strptime(m.group(1), TIMESTAMP_FORMAT)
    except ValueError:
        return None
    return ParsedEvent(
        line_no=line_no, device_ts=ts, er=None, tag=SESSION_START_TAG, subtag=None, fields={}
    )


def _alarm_id(fields: dict) -> str | None:
    for key in ("ID", "alarm"):
        value = fields.get(key)
        if isinstance(value, str) and value:
            return value
    return None
