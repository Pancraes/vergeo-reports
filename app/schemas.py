"""Pydantic models for request bodies and responses. These are also what /docs shows."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from .models import ReportStatus

# Pydantic checks `pattern` before applying `to_lower`, so the pattern must accept both cases;
# the stored value is still lower-case. (Found by test_triage_then_fix_with_multiple_commits.)
CommitSha = Annotated[
    str,
    StringConstraints(strip_whitespace=True, to_lower=True, pattern=r"^[0-9a-fA-F]{7,40}$"),
]


class ReportSummary(BaseModel):
    """A report as listed. Never includes the raw bytes; use GET /reports/{id}/raw for those."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(description="Server-generated id, used in every /reports/{id} URL.")
    report_uuid: uuid.UUID = Field(
        description="Idempotency key: sent by the client, or derived from content_sha256."
    )
    content_sha256: str
    byte_size: int
    app_version: str
    version_code: int
    debug_build: bool | None
    device_model: str
    android_api: int
    description: str = Field(description="What the user wrote, as it appears in the report.")
    status: ReportStatus
    fix_commits: list[str]
    resolution_note: str | None
    event_count: int
    parse_warning_count: int
    trailer_present: bool = Field(
        description="False if the report never reached its REPORT COMPLETE line (truncated)."
    )
    window_start: datetime | None = Field(description="Earliest event, device-local time.")
    window_end: datetime | None = Field(description="Latest event, device-local time.")
    received_at: datetime
    updated_at: datetime


class ParseWarning(BaseModel):
    line_no: int | None
    reason: str


class ReportDetail(ReportSummary):
    parser_version: int
    env: dict[str, str] | None = Field(description="The ENV| line: permission/connectivity flags.")
    diagnostics_included: bool
    trailer_lines: int | None
    window_truncated: bool | None
    parse_warnings: list[ParseWarning] = Field(description="First 200 warnings.")


class ReportPage(BaseModel):
    items: list[ReportSummary]
    total: int
    limit: int
    offset: int


class ReportPatch(BaseModel):
    """Triage update. Omitted fields keep their current value."""

    model_config = ConfigDict(extra="forbid")

    status: ReportStatus | None = None
    fix_commits: list[CommitSha] | None = Field(
        default=None,
        max_length=20,
        description="Git SHAs (7-40 hex chars). Required, non-empty, when status is 'fixed'.",
    )
    resolution_note: str | None = Field(
        default=None,
        max_length=2000,
        description="Required when status is 'closed'. Send null to clear it.",
    )


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    report_id: uuid.UUID
    line_no: int = Field(description="1-based line in the raw report; authoritative order.")
    device_ts: datetime = Field(description="Device-local wall clock, no UTC offset.")
    er: int | None = Field(description="elapsedRealtime ms; monotonic within one boot.")
    tag: str
    subtag: str | None
    alarm_id: str | None
    build_sha: str | None = Field(
        description="Build attributed from TEST_SESSION_START gitSha; null when unknown."
    )
    fields: dict[str, str | list[str]]


class EventPage(BaseModel):
    items: list[EventOut]
    next_cursor: str | None = Field(
        description="Pass as ?cursor= to get the next page; null on the last page."
    )


class Health(BaseModel):
    status: str
    database: str
