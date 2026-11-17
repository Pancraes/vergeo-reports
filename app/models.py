"""ORM models. The Alembic migration in alembic/versions/ is the source of truth for the live
schema; `alembic check` in CI fails if this file and the migrations drift apart.

Column vs JSONB rule used here: a value gets a real column when it is (a) present on most rows,
(b) filtered or sorted on often and (c) stably typed. Everything tag-specific goes in
Event.fields, because the historical reports already contain 125 tags with different keys and the
app adds new ones regularly; a column per key would mean a migration per new log line.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Identity,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


class ReportStatus(enum.StrEnum):
    new = "new"
    triaged = "triaged"
    fixed = "fixed"  # requires fix_commits
    closed = "closed"  # no code fix (not a bug, platform limit, ...); requires resolution_note


class Report(Base):
    __tablename__ = "reports"

    # Server-generated surrogate key, used in URLs and as the events FK.
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # Client idempotency key. Sent by the app, or derived from content_sha256 for legacy
    # reports. UNIQUE is what makes concurrent duplicate uploads safe.
    report_uuid: Mapped[uuid.UUID] = mapped_column(Uuid, unique=True)
    # sha256 of the exact uploaded bytes. Not unique: the same bytes under two client UUIDs is
    # allowed (the app could regenerate a UUID); duplicates are surfaced, not rejected.
    content_sha256: Mapped[str] = mapped_column(String(64), index=True)

    # The source artifact, kept so a newer parser can reprocess history. deferred=True: listing
    # reports never drags megabytes of raw text out of the table.
    raw: Mapped[bytes] = mapped_column(LargeBinary, deferred=True)
    byte_size: Mapped[int] = mapped_column(Integer)
    parser_version: Mapped[int] = mapped_column(Integer)

    # From the APP| header line.
    app_version: Mapped[str] = mapped_column(String(32))
    version_code: Mapped[int] = mapped_column(Integer)
    debug_build: Mapped[bool | None] = mapped_column(Boolean)
    device_model: Mapped[str] = mapped_column(String(128))
    android_api: Mapped[int] = mapped_column(Integer)
    # 11 permission/connectivity flags. Read when triaging, rarely filtered: JSONB, no index.
    env: Mapped[dict | None] = mapped_column(JSONB)
    description: Mapped[str] = mapped_column(Text)

    diagnostics_included: Mapped[bool] = mapped_column(Boolean)
    # False means the report never reached its REPORT COMPLETE line (cut off in transit).
    trailer_present: Mapped[bool] = mapped_column(Boolean)
    trailer_lines: Mapped[int | None] = mapped_column(Integer)
    window_truncated: Mapped[bool | None] = mapped_column(Boolean)

    event_count: Mapped[int] = mapped_column(Integer)
    parse_warning_count: Mapped[int] = mapped_column(Integer)
    parse_warnings: Mapped[list] = mapped_column(JSONB)  # first 200 {line_no, reason}
    # Device-local wall clock (the log has no UTC offset), min/max over the events.
    window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    window_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))

    # VARCHAR + CHECK rather than a native Postgres ENUM: changing the set of statuses later is a
    # plain constraint swap in a migration, whereas a native enum value can never be removed.
    status: Mapped[ReportStatus] = mapped_column(
        Enum(
            ReportStatus,
            native_enum=False,
            length=16,
            create_constraint=True,
            name="report_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        default=ReportStatus.new,
        server_default=ReportStatus.new.value,
    )
    fix_commits: Mapped[list[str]] = mapped_column(
        ARRAY(String(40)), default=list, server_default=text("'{}'")
    )
    resolution_note: Mapped[str | None] = mapped_column(Text)

    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    events: Mapped[list[Event]] = relationship(
        back_populates="report", cascade="all, delete-orphan", passive_deletes=True
    )

    # Fetch server-generated columns (received_at, updated_at) with INSERT ... RETURNING instead
    # of a second SELECT the first time the response reads them.
    __mapper_args__ = {"eager_defaults": True}

    __table_args__ = (
        # The workflow invariants are enforced by the API AND here, so a manual SQL edit or a
        # future code path cannot store a "fixed" report with no commit.
        CheckConstraint(
            "status <> 'fixed' OR cardinality(fix_commits) > 0", name="fixed_requires_commit"
        ),
        CheckConstraint(
            "status <> 'closed' OR length(btrim(coalesce(resolution_note, ''))) > 0",
            name="closed_requires_note",
        ),
    )


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    report_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("reports.id", ondelete="CASCADE"))
    # Physical 1-based line in the raw report: the authoritative order within a report, because
    # device wall-clock time skews and repeats (the 11-06 report crosses the Nov 1 DST change).
    line_no: Mapped[int] = mapped_column(Integer)
    device_ts: Mapped[datetime] = mapped_column(DateTime(timezone=False))
    # elapsedRealtime ms: monotonic within one boot. NULL only on synthetic SESSION_START events.
    er: Mapped[int | None] = mapped_column(BigInteger)
    tag: Mapped[str] = mapped_column(String(64))
    subtag: Mapped[str | None] = mapped_column(String(64))
    alarm_id: Mapped[str | None] = mapped_column(String(64))
    # Attributed from TEST_SESSION_START gitSha, cleared at each process start. NULL = unknown.
    build_sha: Mapped[str | None] = mapped_column(String(40))
    fields: Mapped[dict] = mapped_column(JSONB, default=dict)

    report: Mapped[Report] = relationship(back_populates="events")

    __table_args__ = (
        # Each raw line becomes at most one event; also the index behind per-report timelines
        # (WHERE report_id = ? ORDER BY line_no), their line_no cursor, and the FK cascade.
        UniqueConstraint("report_id", "line_no", name="uq_events_report_line"),
        # Cross-report search almost always names a tag ("every TRIGGER_ALLOWED"), and its keyset
        # pagination orders by (device_ts, id).
        Index("ix_events_tag_ts_id", "tag", "device_ts", "id"),
        # Partial indexes: most events have no build or alarm, so only index rows that do.
        Index("ix_events_build_sha", "build_sha", postgresql_where=text("build_sha IS NOT NULL")),
        Index("ix_events_alarm_id", "alarm_id", postgresql_where=text("alarm_id IS NOT NULL")),
        # Containment queries (fields @> '{"path":"ARRIVAL"}'). jsonb_path_ops is smaller and
        # faster than the default GIN opclass but only supports @>, which is all the API uses.
        Index(
            "ix_events_fields",
            "fields",
            postgresql_using="gin",
            postgresql_ops={"fields": "jsonb_path_ops"},
        ),
    )
