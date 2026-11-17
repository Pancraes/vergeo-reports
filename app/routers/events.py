"""/events: search events across every stored report."""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import require_admin_key
from ..db import get_db
from ..models import Event, Report
from ..schemas import EventOut, EventPage
from ..services.queries import (
    InvalidQuery,
    event_filter_clauses,
    global_cursor_clause,
    global_cursor_for,
    parse_field_filters,
)
from .common import require_device_local

router = APIRouter(prefix="/events", tags=["events"])


@router.get(
    "",
    response_model=EventPage,
    dependencies=[Depends(require_admin_key)],
    summary="Cross-report event search (cursor pagination)",
)
def search_events(
    tag: list[str] | None = Query(
        None, description="Repeatable: ?tag=TRIGGER_ALLOWED&tag=AUDIO_STOP"
    ),
    subtag: str | None = Query(None, description="e.g. FIX for DIAG2|FIX lines"),
    build_sha: str | None = Query(None, description="Events attributed to this build"),
    alarm_id: str | None = Query(None, description="The alarm's opaque UUID (ID= / alarm=)"),
    report_id: uuid.UUID | None = Query(None),
    device: str | None = Query(None, description="Exact device model of the report"),
    app_version: str | None = Query(None, description="APP| ver of the report"),
    version_code: int | None = Query(None),
    ts_from: datetime | None = Query(None, alias="from", description="Device-local, inclusive"),
    ts_to: datetime | None = Query(None, alias="to", description="Device-local, exclusive"),
    field: list[str] | None = Query(
        None,
        description="Repeatable key:value exact match on event fields. "
        "field=path:ARRIVAL finds every ARRIVAL trigger.",
    ),
    cursor: str | None = Query(None, description="next_cursor from the previous page"),
    limit: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> EventPage:
    """Ordered by (device_ts, id), keyset-paginated.

    Reports overlap (the two 2026-10-07 reports share ~2,000 lines), so the same log line can
    appear once per report that contains it. Each result carries report_id and line_no so you can
    tell them apart; see README "Known limitations".
    """
    require_device_local(ts_from, "from")
    require_device_local(ts_to, "to")
    try:
        clauses = event_filter_clauses(
            tags=tag,
            subtag=subtag,
            build_sha=build_sha,
            alarm_id=alarm_id,
            ts_from=ts_from,
            ts_to=ts_to,
            fields=parse_field_filters(field),
        )
        if cursor:
            clauses.append(global_cursor_clause(cursor))
    except InvalidQuery as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(e)) from None

    query = select(Event).where(*clauses)
    if report_id is not None:
        query = query.where(Event.report_id == report_id)
    report_conditions = []
    if device is not None:
        report_conditions.append(Report.device_model == device)
    if app_version is not None:
        report_conditions.append(Report.app_version == app_version)
    if version_code is not None:
        report_conditions.append(Report.version_code == version_code)
    if report_conditions:
        query = query.join(Report, Report.id == Event.report_id).where(*report_conditions)

    rows = db.scalars(query.order_by(Event.device_ts, Event.id).limit(limit + 1)).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    return EventPage(
        items=[EventOut.model_validate(r) for r in rows],
        next_cursor=global_cursor_for(rows[-1]) if has_more else None,
    )
