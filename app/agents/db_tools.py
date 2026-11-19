"""Database wiring for agent runs. Imported only on the database path, because importing app.db
needs DATABASE_URL (the offline path must work without one)."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import func, select

from ..config import get_settings
from ..db import SessionLocal
from ..models import Event
from ..services.queries import event_filter_clauses, parse_field_filters
from .extract import fmt, short_alarm
from .source import PostgresSource


def cross_report_search(session, *, tag: str, fields: list[str], limit: int) -> dict:
    """One tag across every stored report, through the same filter clauses as GET /events."""
    clauses = event_filter_clauses(
        tags=[tag],
        subtag=None,
        build_sha=None,
        alarm_id=None,
        ts_from=None,
        ts_to=None,
        fields=parse_field_filters(fields),
    )
    per_report = session.execute(
        select(Event.report_id, func.count()).where(*clauses).group_by(Event.report_id)
    ).all()
    rows = session.scalars(select(Event).where(*clauses).order_by(Event.device_ts).limit(limit))
    return {
        "tag": tag,
        "filters": fields,
        "reports_matching": len(per_report),
        "total_matching": sum(n for _, n in per_report),
        "sample": [
            {
                "time": fmt(e.device_ts),
                "report_id": str(e.report_id),
                "build": e.build_sha,
                "alarm": short_alarm(e.alarm_id),
                "fields": e.fields,
            }
            for e in rows
        ],
    }


def open_postgres_source(report: Path):
    session = SessionLocal()
    try:
        source = PostgresSource.ingest_file(
            session, report, max_bytes=get_settings().max_upload_bytes
        )
    except BaseException:
        session.close()
        raise

    def cross(**kw):
        return cross_report_search(session, **kw)

    return source, cross, session.close
