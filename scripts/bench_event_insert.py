"""Measure one report's ingest with the ORM bulk insert versus the Core insert the service uses.

This reproduces the measurement behind the comment in app/services/ingest.py. It drops and
re-migrates the database in DATABASE_URL, so it refuses any database whose name lacks "test".

    DATABASE_URL=postgresql+psycopg://vergeo:vergeo@localhost:5433/vergeo_test \
        python -m scripts.bench_event_insert path/to/problem_report.txt [--runs 3]
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import event, insert, text

import app.services.ingest as ingest
from app.db import SessionLocal, engine
from app.models import Event
from app.parser import parse_report


def orm_insert(target):
    """What the service did before: an ORM bulk INSERT against the mapped class."""
    return insert(Event) if target is Event.__table__ else insert(target)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("report", type=Path)
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args()

    if "test" not in (engine.url.database or ""):
        raise SystemExit(f"refusing to wipe {engine.url.database!r}: name must contain 'test'")
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    command.upgrade(Config("alembic.ini"), "head")

    raw = args.report.read_bytes()
    start = time.perf_counter()
    parsed = parse_report(raw)
    parse_s = time.perf_counter() - start
    print(f"{len(raw)} bytes, {len(parsed.events)} events, parse {parse_s:.2f} s")

    executions: list[bool] = []

    def count(conn, cursor, statement, params, context, executemany):
        executions.append(executemany)

    event.listen(engine, "before_cursor_execute", count)
    core_insert = ingest.insert
    try:
        for _ in range(args.runs):
            for label, insert_fn in (("ORM ", orm_insert), ("Core", core_insert)):
                with engine.begin() as conn:
                    conn.execute(text("TRUNCATE reports, events CASCADE"))
                ingest.insert = insert_fn
                executions.clear()
                with SessionLocal() as session:
                    start = time.perf_counter()
                    ingest.ingest_report(session, raw, max_bytes=len(raw))
                    elapsed = time.perf_counter() - start
                print(
                    f"{label} ingest {elapsed:6.2f} s, {len(executions)} cursor executions, "
                    f"{sum(executions)} of them executemany"
                )
    finally:
        ingest.insert = core_insert
        event.remove(engine, "before_cursor_execute", count)


if __name__ == "__main__":
    main()
