"""Ingest historical Vergeo problem reports from the Vergeo repo's incidents/ folder.

Uses app.services.ingest.ingest_report, the same function POST /reports calls, so there is one
parser and one storage path. Re-running is safe: reports have no app-generated UUID yet, so each
one's report_uuid is derived from its content hash and a repeat is an idempotent no-op.

    DATABASE_URL=... INGEST_API_KEY=... ADMIN_API_KEY=... \
        python -m scripts.backfill ../Vergeo/incidents

It prints per-file results, totals, and how much the stored reports overlap (the same log line
present in several reports). Overlap is reported, not removed; see README "Known limitations".
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sqlalchemy import text

from app.config import get_settings
from app.db import SessionLocal
from app.parser import ReportFormatError
from app.services.ingest import ReportConflict, ingest_report

DEFAULT_GLOB = "*/raw/problem_report*.txt"

# Two events are "the same log line" when everything the device wrote matches. er is
# elapsedRealtime, so (er, device_ts, tag) is already near-unique; fields closes the gap.
OVERLAP_SQL = text(
    """
    SELECT a.report_id AS a, b.report_id AS b, count(*) AS shared
    FROM events a
    JOIN events b
      ON a.er = b.er AND a.device_ts = b.device_ts AND a.tag = b.tag
     AND a.fields = b.fields AND a.report_id < b.report_id
    GROUP BY a.report_id, b.report_id
    ORDER BY shared DESC
    """
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("incidents_dir", type=Path, help="Vergeo repo incidents/ folder")
    parser.add_argument("--glob", default=DEFAULT_GLOB, help=f"default: {DEFAULT_GLOB}")
    args = parser.parse_args(argv)

    paths = sorted(args.incidents_dir.glob(args.glob))
    if not paths:
        print(f"no files match {args.glob!r} under {args.incidents_dir}", file=sys.stderr)
        return 1

    limit = get_settings().max_upload_bytes
    created = existing = failed = 0
    names: dict[str, str] = {}

    with SessionLocal() as session:
        for path in paths:
            rel = path.relative_to(args.incidents_dir).as_posix()
            try:
                result = ingest_report(session, path.read_bytes(), max_bytes=limit)
            except (ReportFormatError, ReportConflict) as e:
                failed += 1
                print(f"FAILED   {rel}: {e}")
                continue
            r = result.report
            if result.created:
                names[str(r.id)] = rel
                created += 1
                print(
                    f"created  {rel}: {r.event_count} events, "
                    f"{r.parse_warning_count} warnings, {r.byte_size} bytes"
                )
            else:
                existing += 1
                first = names.get(str(r.id))
                where = f"report from {first}" if first else "a report already in the database"
                print(f"existing {rel}: identical to {where}")

        totals = session.execute(
            text(
                "SELECT count(*), coalesce(sum(event_count), 0), "
                "coalesce(sum(parse_warning_count), 0) FROM reports"
            )
        ).one()
        distinct_tags = session.scalar(text("SELECT count(DISTINCT tag) FROM events"))
        builds = session.execute(
            text(
                "SELECT build_sha, count(*) FROM events WHERE build_sha IS NOT NULL "
                "GROUP BY build_sha ORDER BY count(*) DESC"
            )
        ).all()
        unknown_build = session.scalar(text("SELECT count(*) FROM events WHERE build_sha IS NULL"))
        overlaps = session.execute(OVERLAP_SQL).all()

    print()
    print(f"files: {len(paths)}  created: {created}  already stored: {existing}  failed: {failed}")
    print(
        f"database: {totals[0]} reports, {totals[1]} events, {totals[2]} parse warnings, "
        f"{distinct_tags} distinct tags"
    )
    print(f"events with an attributed build: {sum(n for _, n in builds)}; unknown: {unknown_build}")
    for sha, n in builds:
        print(f"  {sha}: {n}")
    print("report overlap (identical log lines stored under two reports):")
    if not overlaps:
        print("  none")
    for a, b, shared in overlaps:
        label_a, label_b = names.get(str(a), str(a)), names.get(str(b), str(b))
        print(f"  {shared:6d} shared lines: {label_a}  <->  {label_b}")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
