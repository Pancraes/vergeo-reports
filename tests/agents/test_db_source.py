"""The database path reads exactly what the parser path reads, through the API's own clauses."""

from app.agents import extract as x
from app.agents.db_tools import cross_report_search
from app.agents.source import PostgresSource
from app.services.ingest import ingest_report
from tests.agents.ride import ride_report, ride_source
from tests.factory import build_report


def test_postgres_source_matches_the_parsed_source(db):
    report = ingest_report(db, ride_report(), max_bytes=4 * 1024 * 1024).report
    pg = PostgresSource(db, report.id)
    assert [(e.line_no, e.tag) for e in pg.events()] == [
        (e.line_no, e.tag) for e in ride_source().events()
    ]
    assert x.find_rings(pg.events()) == x.find_rings(ride_source().events())
    assert pg.meta.event_count == report.event_count


def test_cross_report_search_counts_every_stored_report(db):
    ingest_report(db, ride_report(), max_bytes=4 * 1024 * 1024)
    ingest_report(db, build_report(), max_bytes=4 * 1024 * 1024)
    out = cross_report_search(db, tag="TRIGGER_ALLOWED", fields=["path:ARRIVAL"], limit=10)
    assert out["reports_matching"] == 2 and out["total_matching"] == 2
