"""Report ingest: idempotency plus one all-or-nothing transaction.

Used by POST /reports and by scripts/backfill.py, so historical and live reports go through the
exact same parser and storage path.

Order of work, and why:

1. size check, sha256, parse   - no database involved. Parsing is the slow, CPU-bound part, so no
                                 connection or lock is held while it runs, and a malformed report
                                 (ReportFormatError -> 422) never opens a transaction.
2. SELECT by report_uuid       - SQLAlchemy begins the transaction here (autobegin).
   exists, same sha256         -> return the existing report (HTTP 200)
   exists, different sha256    -> ReportConflict (HTTP 409)
3. INSERT report, flush        - the UNIQUE(report_uuid) constraint is checked here.
4. bulk INSERT events          - one executemany for all events.
5. COMMIT

Any exception in 3-5 rolls back the whole transaction, so the database never holds a report
without its events. If two uploads of the same report_uuid race, both can pass step 2; the loser
gets an IntegrityError at step 3, rolls back, re-reads the winner's row and resolves to 200/409
exactly as if it had arrived second. The constraint, not the SELECT, is what guarantees one row.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import Event, Report
from ..parser import ParsedReport, parse_report

# Fixed namespace for deriving report_uuid from content for reports the app sent without one.
# Never change it: doing so would give every legacy report a new identity.
LEGACY_REPORT_NAMESPACE = uuid.UUID("6f1d3c7e-2b9a-4c55-9e0e-5a7d8b1c2f30")


class ReportTooLarge(Exception):
    def __init__(self, size: int, limit: int):
        super().__init__(f"report is {size} bytes; the limit is {limit}")
        self.size = size
        self.limit = limit


class ReportConflict(Exception):
    """The report_uuid already exists with different content."""

    def __init__(self, existing: Report):
        super().__init__(
            f"report_uuid {existing.report_uuid} already exists with different content"
        )
        self.existing = existing


@dataclass(frozen=True)
class IngestResult:
    report: Report
    created: bool  # False: an identical report already existed (idempotent retry)


def derive_report_uuid(content_sha256: str) -> uuid.UUID:
    return uuid.uuid5(LEGACY_REPORT_NAMESPACE, content_sha256)


def ingest_report(
    session: Session,
    raw: bytes,
    report_uuid: uuid.UUID | None = None,
    *,
    max_bytes: int,
) -> IngestResult:
    """Store one report and its events, or return the identical one already stored.

    Raises ReportTooLarge, ReportFormatError (from the parser) or ReportConflict.
    """
    if len(raw) > max_bytes:
        raise ReportTooLarge(len(raw), max_bytes)

    content_sha256 = hashlib.sha256(raw).hexdigest()
    parsed = parse_report(raw)  # before any DB work, see module docstring
    if report_uuid is None:
        report_uuid = derive_report_uuid(content_sha256)

    existing = find_by_report_uuid(session, report_uuid)
    if existing is not None:
        # Nothing was written; the read-only transaction ends when the caller closes the session.
        return _resolve_existing(existing, content_sha256)

    report = _new_report(parsed, raw, content_sha256, report_uuid)
    try:
        session.add(report)
        session.flush()  # INSERT reports now, so the events' FK target exists
        rows = _event_rows(report.id, parsed)
        if rows:
            # Core insert on the Table, not the ORM entity. ORM bulk INSERT leaves out None-valued
            # keys so column defaults can apply, which splits rows into a separate batch per NULL
            # pattern (er/subtag/alarm_id/build_sha vary line to line). Measured on the
            # 8,508-event 2026-11-06 report: ORM = 1,404 statements, 38.5 s; Core = one batched
            # executemany, 1.0 s.
            session.execute(insert(Event.__table__), rows)
        session.commit()
    except IntegrityError:
        session.rollback()  # the failed transaction is unusable; start a fresh one for the read
        winner = find_by_report_uuid(session, report_uuid)
        if winner is None:
            raise  # not a report_uuid race (e.g. an event-level constraint): surface it
        return _resolve_existing(winner, content_sha256)
    except BaseException:
        session.rollback()
        raise

    return IngestResult(report=report, created=True)


def find_by_report_uuid(session: Session, report_uuid: uuid.UUID) -> Report | None:
    return session.scalars(select(Report).where(Report.report_uuid == report_uuid)).one_or_none()


def _resolve_existing(existing: Report, content_sha256: str) -> IngestResult:
    if existing.content_sha256 != content_sha256:
        raise ReportConflict(existing)
    return IngestResult(report=existing, created=False)


def _new_report(
    parsed: ParsedReport, raw: bytes, content_sha256: str, report_uuid: uuid.UUID
) -> Report:
    trailer = parsed.trailer
    return Report(
        id=uuid.uuid4(),
        report_uuid=report_uuid,
        content_sha256=content_sha256,
        raw=raw,
        byte_size=len(raw),
        parser_version=parsed.parser_version,
        app_version=parsed.app.version,
        version_code=parsed.app.version_code,
        debug_build=parsed.app.debug,
        device_model=parsed.app.device_model,
        android_api=parsed.app.android_api,
        env=parsed.env,
        description=parsed.description,
        diagnostics_included=parsed.diagnostics_included,
        trailer_present=trailer is not None,
        trailer_lines=trailer.lines if trailer else None,
        window_truncated=trailer.window_truncated if trailer else None,
        event_count=len(parsed.events),
        parse_warning_count=parsed.warning_count,
        parse_warnings=parsed.warnings,
        window_start=parsed.window_start,
        window_end=parsed.window_end,
    )


def _event_rows(report_id: uuid.UUID, parsed: ParsedReport) -> list[dict]:
    return [
        {
            "report_id": report_id,
            "line_no": e.event.line_no,
            "device_ts": e.event.device_ts,
            "er": e.event.er,
            "tag": e.event.tag,
            "subtag": e.event.subtag,
            "alarm_id": e.alarm_id,
            "build_sha": e.build_sha,
            "fields": e.event.fields,
        }
        for e in parsed.events
    ]
