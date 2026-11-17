"""/reports: upload, list, inspect, download and triage problem reports."""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session, undefer

from ..auth import require_admin_key, require_ingest_key
from ..config import Settings, get_settings
from ..db import get_db
from ..models import Event, Report, ReportStatus
from ..parser import ReportFormatError
from ..schemas import EventOut, EventPage, ReportDetail, ReportPage, ReportPatch
from ..services.ingest import ReportConflict, ReportTooLarge, ingest_report
from ..services.queries import (
    InvalidQuery,
    event_filter_clauses,
    parse_field_filters,
    report_cursor_clause,
    report_cursor_for,
)
from ..services.triage import InvalidTransition, InvalidTriage, apply_triage
from .common import require_device_local

router = APIRouter(prefix="/reports", tags=["reports"])

_422 = status.HTTP_422_UNPROCESSABLE_CONTENT


def _get_or_404(db: Session, report_id: uuid.UUID, *options) -> Report:
    report = db.get(Report, report_id, options=list(options))
    if report is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="report not found")
    return report


@router.post(
    "",
    response_model=ReportDetail,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_ingest_key)],
    summary="Upload one Vergeo problem report",
    responses={
        200: {"model": ReportDetail, "description": "Identical report already stored (retry)."},
        401: {"description": "Missing or invalid ingest key."},
        409: {"description": "report_uuid already used for different content."},
        413: {"description": "Upload larger than MAX_UPLOAD_BYTES."},
        422: {"description": "Not a Vergeo problem report (required header missing/invalid)."},
    },
)
def upload_report(
    response: Response,
    file: Annotated[UploadFile, File(description="The problem report .txt, byte for byte.")],
    report_uuid: Annotated[
        uuid.UUID | None,
        Form(description="Client idempotency key. Omit for legacy reports; derived from content."),
    ] = None,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Report:
    """Parse the report into events and store both, in one transaction.

    - unseen `report_uuid` -> **201**
    - same `report_uuid`, same bytes -> **200** with the stored report (safe retry)
    - same `report_uuid`, different bytes -> **409**
    """
    # Read at most limit+1 bytes: enough to know the file is too big without buffering more.
    raw = file.file.read(settings.max_upload_bytes + 1)
    try:
        result = ingest_report(db, raw, report_uuid, max_bytes=settings.max_upload_bytes)
    except ReportTooLarge as e:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, detail=str(e)) from None
    except ReportFormatError as e:
        raise HTTPException(_422, detail=f"not a Vergeo problem report: {e}") from None
    except ReportConflict as e:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "message": "report_uuid already exists with different content",
                "existing_report_id": str(e.existing.id),
            },
        ) from None

    if not result.created:
        response.status_code = status.HTTP_200_OK
    response.headers["Location"] = f"/reports/{result.report.id}"
    return result.report


@router.get(
    "",
    response_model=ReportPage,
    dependencies=[Depends(require_admin_key)],
    summary="List reports (offset pagination)",
)
def list_reports(
    app_version: str | None = Query(None, description="Exact APP| ver, e.g. 1.9.0"),
    version_code: int | None = Query(None),
    device: str | None = Query(
        None, description="Exact device model, e.g. 'motorola moto g(7) play'"
    ),
    status_: ReportStatus | None = Query(None, alias="status"),
    build_sha: str | None = Query(None, description="Reports containing any event from this build"),
    ts_from: datetime | None = Query(
        None,
        alias="from",
        description="Device-local. Reports whose event window ends at/after this.",
    ),
    ts_to: datetime | None = Query(
        None, alias="to", description="Device-local. Reports whose event window starts before this."
    ),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0, le=100_000),
    db: Session = Depends(get_db),
) -> ReportPage:
    """Newest first. Offset pagination is fine here: a few reports a week, and a page of reports
    is something a person scrolls, where a total count is useful."""
    require_device_local(ts_from, "from")
    require_device_local(ts_to, "to")

    conditions = []
    if app_version is not None:
        conditions.append(Report.app_version == app_version)
    if version_code is not None:
        conditions.append(Report.version_code == version_code)
    if device is not None:
        conditions.append(Report.device_model == device)
    if status_ is not None:
        conditions.append(Report.status == status_)
    if build_sha is not None:
        conditions.append(
            exists().where(Event.report_id == Report.id, Event.build_sha == build_sha)
        )
    # Interval overlap: [window_start, window_end] intersects [from, to).
    if ts_from is not None:
        conditions.append(Report.window_end >= ts_from)
    if ts_to is not None:
        conditions.append(Report.window_start < ts_to)

    total = db.scalar(select(func.count()).select_from(Report).where(*conditions))
    items = db.scalars(
        select(Report)
        .where(*conditions)
        .order_by(Report.received_at.desc(), Report.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return ReportPage(items=items, total=total, limit=limit, offset=offset)


@router.get(
    "/{report_id}",
    response_model=ReportDetail,
    dependencies=[Depends(require_admin_key)],
    summary="Get one report's metadata and parse results",
    responses={404: {"description": "No such report."}},
)
def get_report(report_id: uuid.UUID, db: Session = Depends(get_db)) -> Report:
    return _get_or_404(db, report_id)


@router.get(
    "/{report_id}/raw",
    dependencies=[Depends(require_admin_key)],
    summary="Download the original report bytes",
    response_class=Response,
    responses={
        200: {"content": {"text/plain": {}}, "description": "The report exactly as uploaded."},
        404: {"description": "No such report."},
    },
)
def get_report_raw(report_id: uuid.UUID, db: Session = Depends(get_db)) -> Response:
    report = _get_or_404(db, report_id, undefer(Report.raw))
    return Response(
        content=report.raw,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="report-{report.id}.txt"'},
    )


@router.patch(
    "/{report_id}",
    response_model=ReportDetail,
    dependencies=[Depends(require_admin_key)],
    summary="Triage: change status, fix commits or resolution note",
    responses={
        404: {"description": "No such report."},
        409: {"description": "Transition not allowed from the current status."},
        422: {"description": "fixed without a commit, closed without a note, or a bad SHA."},
    },
)
def patch_report(report_id: uuid.UUID, body: ReportPatch, db: Session = Depends(get_db)) -> Report:
    """Allowed moves: new -> triaged/fixed/closed, triaged -> fixed/closed,
    fixed -> triaged and closed -> triaged (reopen). `fixed` needs at least one commit,
    `closed` needs a resolution_note."""
    if not body.model_fields_set:
        raise HTTPException(_422, detail="PATCH body must set at least one field")
    report = _get_or_404(db, report_id)
    try:
        apply_triage(
            report,
            status=body.status,
            fix_commits=body.fix_commits,
            resolution_note=body.resolution_note,
            note_provided="resolution_note" in body.model_fields_set,
        )
    except InvalidTransition as e:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(e)) from None
    except InvalidTriage as e:
        db.rollback()
        raise HTTPException(_422, detail=str(e)) from None
    db.commit()
    return report


@router.get(
    "/{report_id}/events",
    response_model=EventPage,
    dependencies=[Depends(require_admin_key)],
    summary="One report's event timeline, in line order",
    responses={404: {"description": "No such report."}},
)
def report_events(
    report_id: uuid.UUID,
    tag: list[str] | None = Query(
        None, description="Repeatable: ?tag=ALARM_EVAL&tag=TRIGGER_ALLOWED"
    ),
    subtag: str | None = Query(None),
    alarm_id: str | None = Query(None),
    ts_from: datetime | None = Query(None, alias="from", description="Device-local, inclusive"),
    ts_to: datetime | None = Query(None, alias="to", description="Device-local, exclusive"),
    field: list[str] | None = Query(
        None, description="Repeatable key:value exact match on event fields, e.g. path:ARRIVAL"
    ),
    cursor: str | None = Query(None),
    limit: int = Query(200, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> EventPage:
    """Ordered by line_no (file order), the only ordering that survives clock skew and DST."""
    require_device_local(ts_from, "from")
    require_device_local(ts_to, "to")
    _get_or_404(db, report_id)
    try:
        clauses = event_filter_clauses(
            tags=tag,
            subtag=subtag,
            build_sha=None,
            alarm_id=alarm_id,
            ts_from=ts_from,
            ts_to=ts_to,
            fields=parse_field_filters(field),
        )
        if cursor:
            clauses.append(report_cursor_clause(cursor))
    except InvalidQuery as e:
        raise HTTPException(_422, detail=str(e)) from None

    rows = db.scalars(
        select(Event)
        .where(Event.report_id == report_id, *clauses)
        .order_by(Event.line_no)
        .limit(limit + 1)  # one extra row tells us whether another page exists
    ).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    return EventPage(
        items=[EventOut.model_validate(r) for r in rows],
        next_cursor=report_cursor_for(rows[-1]) if has_more else None,
    )
