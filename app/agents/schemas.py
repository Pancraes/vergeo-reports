"""Structured results of the incident agents.

IncidentAnalysis is what the triage agent must return. It is also the JSON schema handed to the
model as its output format, so the model cannot answer in free prose: the SDK validates the shape,
and app.agents.rules then checks it against facts computed from the log without the model.

The verdict vocabulary comes from how Vergeo field rides were actually classified by hand
(docs/ml/FIELD_CORPUS.md in the Vergeo repo), not from a bug / not-bug binary:

- structural_success       rang near the stop because the logic had real evidence (a fix inside
                           the radius, or a checkpoint fix on a direct remainder)
- accidental_success       rang near the stop, but only because errors cancelled (for example a
                           straight-line projection whose anchor happened to be reset late)
- early_false_alarm        rang well before the stop
- late_due_to_complete_blackout
                           no location at all from the platform to the surface, so the first fix
                           after surfacing was the earliest possible ring (the product boundary)
- acquisition_failure      location was available in principle but the app did not get it
- known_platform_limit     a documented device/OS limit decided the outcome (Doze, Battery Saver,
                           GNSS cold start without assistance data)
- software_defect          the app's own state or logic produced the wrong outcome and it is not
                           an early ring (for example: never rang because of a stuck state)
- insufficient_evidence    the report cannot support a classification
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class Verdict(StrEnum):
    structural_success = "structural_success"
    accidental_success = "accidental_success"
    early_false_alarm = "early_false_alarm"
    late_due_to_complete_blackout = "late_due_to_complete_blackout"
    acquisition_failure = "acquisition_failure"
    known_platform_limit = "known_platform_limit"
    software_defect = "software_defect"
    insufficient_evidence = "insufficient_evidence"


SUCCESS_VERDICTS = {Verdict.structural_success, Verdict.accidental_success}


class TriggerPath(StrEnum):
    """What actually produced the full alarm, read from the TRIGGER_ALLOWED line that rang.

    Blind-hop rings are logged as TRIGGER_ALLOWED|reason=TIME_BACKSTOP|tier=blind_motion (a shared
    fire-reason label), so the tier, not the reason, separates blind-hop from the time backstop.
    """

    arrival = "ARRIVAL"
    time_backstop_strong = "TIME_BACKSTOP_STRONG"
    time_backstop_weak = "TIME_BACKSTOP_WEAK"
    blind_hop = "BLIND_HOP"
    near_stop = "NEAR_STOP"
    other = "OTHER"
    none = "NONE"  # never rang


class FailureClass(StrEnum):
    """Why the outcome was what it was, separate from what the outcome was."""

    none = "none"  # nothing went wrong
    software_defect = "software_defect"  # the app's logic or state was wrong
    known_limit = "known_limit"  # the documented information boundary or a platform limit
    environmental = "environmental"  # a disruption outside the app (a held train, user error)
    unclear = "unclear"


class Confidence(StrEnum):
    high = "high"
    medium = "medium"
    low = "low"


class Evidence(BaseModel):
    time: str | None = Field(None, description="Phone-clock time, HH:MM:SS[.fff]")
    tag: str | None = Field(None, description="Log tag the evidence comes from")
    detail: str = Field(description="What the line shows, in one sentence")


class CausalStep(BaseModel):
    time: str | None = None
    step: str


class TimingFact(BaseModel):
    """One timing fact from the rider's notes (see app.agents.laps)."""

    label: str = Field(description="What happened, in the rider's words")
    role: str = Field(
        description="start, boarding, train_moving, station, doors, exit, ring, other"
    )
    stopwatch_offset_s: float | None = Field(
        None, description="Seconds after the first lap, when the note gives it"
    )
    rider_clock: str | None = Field(None, description="Rider clock HH:MM:SS, when known")
    phone_clock: str | None = Field(None, description="Inferred phone clock HH:MM:SS")


class LapAlignment(BaseModel):
    original_note: str | None = None
    facts: list[TimingFact] = Field(default_factory=list)
    phone_minus_rider_s: float | None = Field(
        None, description="Estimated phone clock minus rider clock, seconds"
    )
    uncertainty_s: float | None = Field(None, description="+/- seconds on that offset")
    method: str | None = Field(None, description="How the offset was estimated")


class IncidentAnalysis(BaseModel):
    report_id: str
    alarm: str | None = Field(None, description="Short alarm hash, never the raw id")
    verdict: Verdict
    confidence: Confidence
    failure_class: FailureClass
    trip_direction: str | None = Field(None, description="e.g. southbound; only if inferable")
    origin: str | None = None
    destination: str | None = None

    alarm_arm_time: str | None = Field(None, description="Phone clock")
    departure_time: str | None = Field(None, description="Phone clock, train starts moving")
    estimated_arrival_or_doors_time: str | None = Field(None, description="Phone clock")
    alarm_time: str | None = Field(None, description="Phone clock of ESCALATION_START, or null")
    early_late_seconds: float | None = Field(
        None, description="alarm_time minus doors time: negative = early, positive = late"
    )
    trigger_path: TriggerPath

    last_location_before_blackout: str | None = Field(
        None, description="Time, provider, accuracy and distance of the last fix"
    )
    blackout_duration_s: float | None = Field(
        None, description="Longest gap with no location during the ride, seconds"
    )
    first_location_after_blackout: str | None = None

    route_state_summary: str | None = Field(
        None, description="Route record / fold / spread / holds over the ride"
    )
    fallback_owner_history: str | None = Field(
        None, description="Which safety-net tier owned the trip when, and why"
    )
    ml_summary: str | None = Field(None, description="R4 shadow: predictions, gates, authority")

    lap_alignment: LapAlignment | None = None
    causal_chain: list[CausalStep] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    similar_incidents: list[str] = Field(
        default_factory=list, description="FIELD_CORPUS section titles that match"
    )

    suspected_defect: bool = False
    defect_hypothesis: str | None = None
    needs_fix_investigation: bool = False
    items_to_inspect: list[str] = Field(
        default_factory=list,
        description="Anomalies worth a closer look that are NOT claimed as defects",
    )
    limitations: list[str] = Field(default_factory=list)


class RunMetrics(BaseModel):
    """Measured cost of one agent run. Nothing here is estimated."""

    wall_seconds: float
    tool_calls: int
    tool_calls_by_name: dict[str, int] = Field(default_factory=dict)
    model_turns: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cost_usd: float | None = Field(None, description="As reported by the SDK")
    events_returned: int = Field(0, description="Structured event rows handed to the model")
    bytes_to_model: int = Field(0, description="Tool output bytes sent to the model")
    model: str | None = None
    repair_rounds: int = 0


class TriageResult(BaseModel):
    analysis: IncidentAnalysis | None
    rule_violations: list[str] = Field(default_factory=list)
    metrics: RunMetrics
    error: str | None = None
