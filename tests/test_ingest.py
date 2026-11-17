"""Ingest service against real Postgres: idempotency, transactions, concurrency."""

import threading
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.db import SessionLocal
from app.models import Event, Report
from app.parser import ReportFormatError
from app.services import ingest
from app.services.ingest import (
    ReportConflict,
    ReportTooLarge,
    derive_report_uuid,
    ingest_report,
)

from .factory import DEFAULT_LINES, build_report

LIMIT = 4 * 1024 * 1024


def counts(db):
    db.rollback()  # see rows committed by other sessions
    return (
        db.scalar(select(func.count()).select_from(Report)),
        db.scalar(select(func.count()).select_from(Event)),
    )


def test_new_report_stores_report_and_all_events(db):
    raw = build_report()
    result = ingest_report(db, raw, max_bytes=LIMIT)
    assert result.created is True
    r = result.report
    assert r.event_count == len(DEFAULT_LINES) - 2  # the session block's APP/ENV lines fold in
    assert r.byte_size == len(raw)
    assert r.trailer_present is True
    assert r.status == "new" and r.fix_commits == []
    assert counts(db) == (1, r.event_count)


def test_raw_bytes_are_stored_exactly(db):
    raw = build_report(description="exact bytes é")
    rid = ingest_report(db, raw, max_bytes=LIMIT).report.id
    db.expire_all()
    assert db.get(Report, rid).raw == raw


def test_identical_retry_returns_existing(db):
    key = uuid.uuid4()
    first = ingest_report(db, build_report(), key, max_bytes=LIMIT)
    second = ingest_report(db, build_report(), key, max_bytes=LIMIT)
    assert second.created is False
    assert second.report.id == first.report.id
    assert counts(db)[0] == 1


def test_same_uuid_different_content_conflicts(db):
    key = uuid.uuid4()
    first = ingest_report(db, build_report(description="a"), key, max_bytes=LIMIT)
    with pytest.raises(ReportConflict) as exc:
        ingest_report(db, build_report(description="b"), key, max_bytes=LIMIT)
    assert exc.value.existing.id == first.report.id
    assert counts(db)[0] == 1


def test_legacy_report_gets_uuid_derived_from_content(db):
    raw = build_report()
    r = ingest_report(db, raw, max_bytes=LIMIT).report
    assert r.report_uuid == derive_report_uuid(r.content_sha256)
    # Deterministic: re-uploading the same legacy bytes is an idempotent retry.
    again = ingest_report(db, raw, max_bytes=LIMIT)
    assert again.created is False and again.report.id == r.id


def test_derived_uuid_is_stable_across_runs():
    # Changing LEGACY_REPORT_NAMESPACE would silently re-identify every legacy report.
    assert derive_report_uuid("0" * 64) == uuid.UUID("ab0ab1bf-de40-5148-b68e-1cab1a87f406")


def test_same_bytes_under_two_client_uuids_are_two_reports(db):
    raw = build_report()
    a = ingest_report(db, raw, uuid.uuid4(), max_bytes=LIMIT).report
    b = ingest_report(db, raw, uuid.uuid4(), max_bytes=LIMIT).report
    assert a.id != b.id and a.content_sha256 == b.content_sha256


def test_malformed_header_rejected_before_any_db_write(db):
    with pytest.raises(ReportFormatError):
        ingest_report(db, b"not a report\n", max_bytes=LIMIT)
    assert counts(db) == (0, 0)


def test_oversized_report_rejected(db):
    with pytest.raises(ReportTooLarge):
        ingest_report(db, b"x" * 101, max_bytes=100)
    assert counts(db) == (0, 0)


def test_event_insert_failure_rolls_back_the_report_too(db, monkeypatch):
    real_rows = ingest._event_rows

    def duplicate_line_numbers(report_id, parsed):
        rows = real_rows(report_id, parsed)
        rows[-1]["line_no"] = rows[0]["line_no"]  # violates UNIQUE(report_id, line_no)
        return rows

    monkeypatch.setattr(ingest, "_event_rows", duplicate_line_numbers)
    with pytest.raises(IntegrityError):
        ingest_report(db, build_report(), max_bytes=LIMIT)
    assert counts(db) == (0, 0)  # the report row inserted before the events is gone too


def test_non_integrity_failure_also_rolls_back(db, monkeypatch):
    real_rows = ingest._event_rows

    def broken(report_id, parsed):
        rows = real_rows(report_id, parsed)
        rows[3]["er"] = "not-a-number"  # psycopg/Postgres rejects this mid-batch
        return rows

    monkeypatch.setattr(ingest, "_event_rows", broken)
    with pytest.raises(Exception):  # noqa: B017 - DataError/StatementError, either way: rollback
        ingest_report(db, build_report(), max_bytes=LIMIT)
    assert counts(db) == (0, 0)


def test_lost_race_resolves_through_the_unique_constraint(db, monkeypatch):
    """Simulate the race deterministically: the optimistic SELECT misses a row another session
    has already committed. The INSERT must hit UNIQUE(report_uuid), and ingest must return the
    existing report instead of an error."""
    key = uuid.uuid4()
    raw = build_report()
    with SessionLocal() as other:
        winner = ingest_report(other, raw, key, max_bytes=LIMIT).report

    real_find = ingest.find_by_report_uuid
    calls = {"n": 0}

    def stale_first_lookup(session, report_uuid):
        calls["n"] += 1
        return None if calls["n"] == 1 else real_find(session, report_uuid)

    monkeypatch.setattr(ingest, "find_by_report_uuid", stale_first_lookup)
    result = ingest_report(db, raw, key, max_bytes=LIMIT)
    assert calls["n"] == 2  # SELECT, failed INSERT, SELECT again
    assert result.created is False and result.report.id == winner.id

    calls["n"] = 0
    with pytest.raises(ReportConflict):
        ingest_report(db, build_report(description="different"), key, max_bytes=LIMIT)
    assert counts(db)[0] == 1


def test_concurrent_duplicate_uploads_store_exactly_one_report(monkeypatch):
    """Two real threads, two connections. A barrier holds both after their optimistic SELECT so
    both are guaranteed to attempt the INSERT; Postgres makes the second wait on the unique index
    and then fail, and the service turns that into an idempotent 'already exists'."""
    key = uuid.uuid4()
    raw = build_report()
    barrier = threading.Barrier(2, timeout=10)
    local = threading.local()
    real_find = ingest.find_by_report_uuid

    def find_then_wait(session, report_uuid):
        found = real_find(session, report_uuid)
        if not getattr(local, "waited", False):
            local.waited = True
            barrier.wait()
        return found

    monkeypatch.setattr(ingest, "find_by_report_uuid", find_then_wait)
    results, errors = [], []

    def upload():
        try:
            with SessionLocal() as s:
                results.append(ingest_report(s, raw, key, max_bytes=LIMIT))
        except Exception as e:  # pragma: no cover - surfaced by the assert below
            errors.append(e)

    threads = [threading.Thread(target=upload) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert errors == []
    assert sorted(r.created for r in results) == [False, True]
    assert len({r.report.id for r in results}) == 1
    with SessionLocal() as s:
        assert counts(s)[0] == 1


def test_events_are_inserted_in_one_batched_statement(db):
    """Regression: ORM bulk insert split rows into one batch per NULL pattern (1,403 executions,
    about 39 s, for the real 8,508-event report). The Core insert must send one executemany."""
    from sqlalchemy import event

    from app.db import engine

    # Vary which nullable columns are NULL from line to line, like real reports do.
    lines = []
    for i in range(300):
        extra = ["|ID=a", "", "|alarm=b", ""][i % 4]
        sub = "FIX|" if i % 3 == 0 else ""
        lines.append(f"2026-11-06 17:00:00.000|er={i}|DIAG2|{sub}k={i}{extra}")
    lines.insert(150, "==== VERGEO SESSION START 2026-11-06 17:00:00.000 ====")  # er NULL

    executemany_calls = []

    def count(conn, cursor, statement, params, context, executemany):
        if statement.lstrip().upper().startswith("INSERT INTO EVENTS"):
            executemany_calls.append(executemany)

    event.listen(engine, "before_cursor_execute", count)
    try:
        result = ingest_report(db, build_report(lines=lines), max_bytes=LIMIT)
    finally:
        event.remove(engine, "before_cursor_execute", count)
    assert result.report.event_count == 301
    assert executemany_calls == [True]
