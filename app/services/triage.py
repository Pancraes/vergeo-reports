"""Status workflow for PATCH /reports/{id}.

The states come from how Vergeo incidents actually end:

    new ──> triaged ──> fixed    (a code fix; may take several commits, e.g. 11-12: three)
     │         │  ^
     │         └─>│closed      (no code fix: "not a bug", "platform limit")
     └─> fixed / closed directly, when the diagnosis is immediate

    fixed  -> triaged   a regression reopens it
    closed -> triaged   new evidence reopens it

Staying in the same state is allowed, to edit fix_commits or resolution_note.

Two kinds of failure map to two status codes:
- InvalidTransition (409): the request is well-formed but conflicts with the report's current
  state (e.g. closed -> fixed without reopening first).
- InvalidTriage (422): the resulting state is invalid (fixed with no commit, closed with no note).
The DB CHECK constraints in models.Report enforce the same two invariants a second time.
"""

from __future__ import annotations

from ..models import Report, ReportStatus

S = ReportStatus

ALLOWED_TRANSITIONS: dict[ReportStatus, set[ReportStatus]] = {
    S.new: {S.new, S.triaged, S.fixed, S.closed},
    S.triaged: {S.triaged, S.fixed, S.closed},
    S.fixed: {S.fixed, S.triaged},
    S.closed: {S.closed, S.triaged},
}


class InvalidTransition(Exception):
    pass


class InvalidTriage(Exception):
    pass


def apply_triage(
    report: Report,
    *,
    status: ReportStatus | None,
    fix_commits: list[str] | None,
    resolution_note: str | None,
    note_provided: bool,
) -> None:
    """Validate and apply a PATCH. Fields not supplied keep their current values.

    note_provided distinguishes "resolution_note omitted" from "resolution_note: null" (clear it).
    """
    new_status = status if status is not None else report.status
    if new_status not in ALLOWED_TRANSITIONS[report.status]:
        raise InvalidTransition(f"cannot move a report from {report.status} to {new_status}")

    new_commits = fix_commits if fix_commits is not None else list(report.fix_commits)
    new_note = resolution_note if note_provided else report.resolution_note

    if new_status == S.fixed and not new_commits:
        raise InvalidTriage("status 'fixed' requires at least one fix_commits entry")
    if new_status == S.closed and not (new_note and new_note.strip()):
        raise InvalidTriage("status 'closed' requires a resolution_note")

    report.status = new_status
    report.fix_commits = new_commits
    report.resolution_note = new_note
