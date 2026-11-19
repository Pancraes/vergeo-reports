"""Invariants the triage answer must satisfy, checked against facts computed from the log.

The model writes the analysis; this module decides whether it is consistent with what the log
says. A violation is sent back to the model once for repair, and any that remain are reported
next to the analysis instead of being silently accepted.

The rules encode the field corpus's own distinctions:

- the trigger path is whatever the ringing TRIGGER_ALLOWED line says, not an interpretation;
- "early" and "late" must agree with the sign of early_late_seconds;
- late_due_to_complete_blackout requires an actual blackout, and is not by itself a defect
  (the 2026-11-10 reliability contract: with zero location from platform to surface, the first
  fix after surfacing is the earliest possible ring);
- a success needs a ring; a fix investigation needs a written defect hypothesis.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from . import extract as x
from .schemas import SUCCESS_VERDICTS, FailureClass, IncidentAnalysis, TriggerPath, Verdict
from .source import Ev

# Below this a gap is ordinary fix spacing, not a blackout worth explaining a late ring with.
BLACKOUT_MIN_S = 600


@dataclass
class Facts:
    rings: list[dict]
    ring_path: str  # TriggerPath value of the first ring in the window, or NONE
    longest_gap_s: float
    gaps_s: list[float]  # every location gap >= 60 s, including the open one ending at the ring


def facts_for(evs: list[Ev]) -> Facts:
    rings = x.find_rings(evs)
    rows = x.fixes(evs)
    gaps = [g["duration_s"] for g in x.gaps(rows, min_gap_s=60)]
    # A ring before the next fix closes a gap the fix list alone never sees (2026-11-09: last
    # fix 16:43:48, time-backstop ring 16:52:15, the next fix only after the stop).
    if rings and rows:
        ring_t = x.parse_ts(rings[0]["time"])
        before = [t for t in (x.parse_ts(f["time"]) for f in rows) if t <= ring_t]
        if before and (ring_t - before[-1]).total_seconds() >= 60:
            gaps.append(round((ring_t - before[-1]).total_seconds(), 1))
    return Facts(
        rings=rings,
        ring_path=rings[0]["trigger_path"] if rings else TriggerPath.none.value,
        longest_gap_s=max(gaps, default=0.0),
        gaps_s=gaps,
    )


def check(a: IncidentAnalysis, f: Facts) -> list[str]:
    v: list[str] = []
    if a.trigger_path.value != f.ring_path:
        v.append(
            f"trigger_path is {a.trigger_path.value} but the log's ringing line says {f.ring_path}"
        )
    if a.verdict in SUCCESS_VERDICTS and not f.rings:
        v.append(f"verdict {a.verdict.value} needs a ring, and the window has none")
    if a.early_late_seconds is not None:
        if a.verdict == Verdict.early_false_alarm and a.early_late_seconds >= 0:
            v.append("early_false_alarm with early_late_seconds >= 0 (positive means late)")
        if a.verdict == Verdict.late_due_to_complete_blackout and a.early_late_seconds <= 0:
            v.append("late_due_to_complete_blackout with early_late_seconds <= 0")
    if a.verdict == Verdict.late_due_to_complete_blackout:
        if f.longest_gap_s < BLACKOUT_MIN_S:
            v.append(
                f"late_due_to_complete_blackout but the longest location gap is only "
                f"{f.longest_gap_s:.0f} s"
            )
        if a.failure_class == FailureClass.software_defect and not a.defect_hypothesis:
            v.append("a complete-blackout late ring is not a defect unless a hypothesis says why")
    if a.blackout_duration_s is not None and a.blackout_duration_s >= 60 and f.gaps_s:
        # A session can hold idle gaps before the ride (hours downtown before an evening ride),
        # so the claim must match SOME gap in the log, not necessarily the longest one.
        if not any(abs(a.blackout_duration_s - g) <= max(120.0, 0.1 * g) for g in f.gaps_s):
            v.append(
                f"blackout_duration_s {a.blackout_duration_s:.0f} matches no location gap in "
                f"the log (largest: {sorted(round(g) for g in f.gaps_s)[-5:]} s)"
            )
    if a.needs_fix_investigation and not (a.suspected_defect and a.defect_hypothesis):
        v.append("needs_fix_investigation requires suspected_defect and a defect_hypothesis")
    if a.alarm_time and f.rings:
        try:
            claimed = _clock(a.alarm_time, x.parse_ts(f.rings[0]["time"]))
            if abs((claimed - x.parse_ts(f.rings[0]["time"])).total_seconds()) > 5:
                v.append(f"alarm_time {a.alarm_time} is not the logged ring {f.rings[0]['time']}")
        except ValueError:
            v.append(f"alarm_time {a.alarm_time!r} is not a time")
    return v


def _clock(text: str, ref: datetime) -> datetime:
    """Accept a full timestamp or a bare HH:MM:SS[.fff] on the reference day."""
    text = text.strip()
    if len(text) <= 12:
        parts = text.split(":")
        h, m = int(parts[0]), int(parts[1])
        s = float(parts[2]) if len(parts) > 2 else 0.0
        return ref.replace(hour=h, minute=m, second=int(s), microsecond=int((s % 1) * 1e6))
    return x.parse_ts(text.replace("T", " "))
