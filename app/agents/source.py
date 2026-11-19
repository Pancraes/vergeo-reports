"""Where the agent tools read events from.

Two implementations of one small interface:

- PostgresSource: the normal path. The report is ingested with app.services.ingest.ingest_report
  (the function POST /reports uses) and read back with the same filter clauses as GET /events
  (app.services.queries.event_filter_clauses), so the agent sees exactly what the API stores.
- ParsedSource: the same parser output held in memory, for unit tests and for running the
  evaluation without a database. It goes through app.parser.parse_report, so there is still one
  parser.

Both hand the tools plain Ev rows, never ORM objects, so nothing the tools return can lazy-load
the raw report bytes.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol

from ..parser import ParsedReport, parse_report


@dataclass(frozen=True)
class Ev:
    line_no: int
    ts: datetime
    tag: str
    subtag: str | None
    fields: dict
    alarm_id: str | None = None
    build_sha: str | None = None

    def get(self, key: str, default: str | None = None) -> str | None:
        value = self.fields.get(key, default)
        if isinstance(value, list):  # repeated key: the first value is the one most lines mean
            return value[0] if value else default
        return value

    def num(self, key: str) -> float | None:
        try:
            return float(self.get(key))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None


@dataclass
class ReportMeta:
    report_id: str
    app_version: str
    version_code: int
    device_model: str
    android_api: int
    env: dict | None
    description: str
    window_start: datetime | None
    window_end: datetime | None
    event_count: int
    extra: dict = field(default_factory=dict)


class EventSource(Protocol):
    meta: ReportMeta

    def events(
        self,
        *,
        tags: Iterable[str] | None = None,
        ts_from: datetime | None = None,
        ts_to: datetime | None = None,
    ) -> list[Ev]: ...


class ParsedSource:
    def __init__(self, parsed: ParsedReport, report_id: str):
        self._events = [
            Ev(
                line_no=e.event.line_no,
                ts=e.event.device_ts,
                tag=e.event.tag,
                subtag=e.event.subtag,
                fields=e.event.fields,
                alarm_id=e.alarm_id,
                build_sha=e.build_sha,
            )
            for e in parsed.events
        ]
        self.meta = ReportMeta(
            report_id=report_id,
            app_version=parsed.app.version,
            version_code=parsed.app.version_code,
            device_model=parsed.app.device_model,
            android_api=parsed.app.android_api,
            env=parsed.env,
            description=parsed.description,
            window_start=parsed.window_start,
            window_end=parsed.window_end,
            event_count=len(self._events),
        )

    @classmethod
    def from_file(cls, path: Path) -> ParsedSource:
        raw = path.read_bytes()
        # Not the ingest report_uuid: computing that would import app.db, which needs a database
        # URL. A content prefix is enough to tell offline runs apart.
        return cls(parse_report(raw), "offline-" + hashlib.sha256(raw).hexdigest()[:12])

    def events(self, *, tags=None, ts_from=None, ts_to=None) -> list[Ev]:
        wanted = set(tags) if tags else None
        return [
            e
            for e in self._events
            if (wanted is None or e.tag in wanted)
            and (ts_from is None or e.ts >= ts_from)
            and (ts_to is None or e.ts < ts_to)
        ]


class PostgresSource:
    """Reads one stored report through the API's own query clauses."""

    def __init__(self, session, report_id: uuid.UUID):
        from ..models import Report

        self._session = session
        self._report_id = report_id
        report = session.get(Report, report_id)
        if report is None:
            raise LookupError(f"no report {report_id}")
        self.meta = ReportMeta(
            report_id=str(report.id),
            app_version=report.app_version,
            version_code=report.version_code,
            device_model=report.device_model,
            android_api=report.android_api,
            env=report.env,
            description=report.description,
            window_start=report.window_start,
            window_end=report.window_end,
            event_count=report.event_count,
        )

    @classmethod
    def ingest_file(cls, session, path: Path, *, max_bytes: int) -> PostgresSource:
        from ..services.ingest import ingest_report

        result = ingest_report(session, path.read_bytes(), max_bytes=max_bytes)
        return cls(session, result.report.id)

    def events(self, *, tags=None, ts_from=None, ts_to=None) -> list[Ev]:
        from sqlalchemy import select

        from ..models import Event
        from ..services.queries import event_filter_clauses

        clauses = event_filter_clauses(
            tags=list(tags) if tags else None,
            subtag=None,
            build_sha=None,
            alarm_id=None,
            ts_from=ts_from,
            ts_to=ts_to,
            fields=[],
        )
        stmt = (
            select(Event)
            .where(Event.report_id == self._report_id, *clauses)
            .order_by(Event.line_no)
        )
        return [
            Ev(
                line_no=row.line_no,
                ts=row.device_ts,
                tag=row.tag,
                subtag=row.subtag,
                fields=row.fields,
                alarm_id=row.alarm_id,
                build_sha=row.build_sha,
            )
            for row in self._session.scalars(stmt)
        ]
