"""The triage loop with a scripted model: schema validation, rule checks and repair rounds.

No network: the Session is a fake that returns canned replies and can call tools the way the real
agent does, so tool counting and auditing are exercised too."""

import asyncio

import pytest

from app.agents.corpus import CorpusIndex
from app.agents.privacy import AuditLog
from app.agents.rules import check, facts_for
from app.agents.schemas import IncidentAnalysis
from app.agents.tools import ToolBox
from app.agents.triage import ModelReply, parse_analysis, run_triage
from tests.agents.ride import ride_source

GOOD = {
    "report_id": "test-report",
    "alarm": "alarm-a1b2c3",
    "verdict": "late_due_to_complete_blackout",
    "confidence": "high",
    "failure_class": "known_limit",
    "alarm_time": "2026-01-10 10:40:26.877",
    "estimated_arrival_or_doors_time": "2026-01-10 10:38:55",
    "early_late_seconds": 92,
    "trigger_path": "ARRIVAL",
    "blackout_duration_s": 2306,
    "items_to_inspect": ["5.4 s STATION_ROUTE_AMBIGUOUS hold on the first surfacing fix"],
}


def box() -> ToolBox:
    b = ToolBox(source=ride_source(), corpus=CorpusIndex([]), audit=AuditLog(None))
    b.default_focus()
    return b


class Scripted:
    def __init__(self, b: ToolBox, replies: list[ModelReply], tools=("get_rings",)):
        self.b, self.replies, self.tools, self.prompts = b, list(replies), tools, []

    async def __call__(self, prompt: str) -> ModelReply:
        self.prompts.append(prompt)
        for t in self.tools:
            self.b.call(t, {})
        return self.replies.pop(0)


def run(b, session, notes="lap started 15:05"):
    return asyncio.run(run_triage(b, session, notes=notes, model="fake"))


def test_a_consistent_answer_needs_no_repair():
    b = box()
    r = run(b, Scripted(b, [ModelReply(structured=GOOD, text=None, turns=5, cost_usd=0.1)]))
    assert r.analysis.verdict.value == "late_due_to_complete_blackout"
    assert r.rule_violations == []
    assert r.metrics.repair_rounds == 0
    assert r.metrics.tool_calls == 1 and r.metrics.model_turns == 5


def test_malformed_output_gets_one_retry():
    b = box()
    s = Scripted(
        b,
        [
            ModelReply(structured={"verdict": "maybe"}, text=None),
            ModelReply(structured=GOOD, text=None),
        ],
    )
    r = run(b, s)
    assert r.analysis is not None and r.metrics.repair_rounds == 1
    assert "not a valid IncidentAnalysis" in s.prompts[1]


def test_json_in_plain_text_is_accepted_when_structured_output_is_missing():
    import json

    a, err = parse_analysis(ModelReply(structured=None, text="here:\n" + json.dumps(GOOD)))
    assert err is None and a.trigger_path.value == "ARRIVAL"


def test_persistently_malformed_output_is_an_error_not_a_guess():
    b = box()
    r = run(
        b,
        Scripted(
            b,
            [
                ModelReply(structured=None, text="no idea"),
                ModelReply(structured=None, text="still no", error="error_max_turns"),
            ],
        ),
    )
    assert r.analysis is None
    assert "error_max_turns" in r.error


def test_a_fact_violation_is_sent_back_and_repaired():
    b = box()
    wrong = {**GOOD, "trigger_path": "BLIND_HOP"}
    s = Scripted(
        b, [ModelReply(structured=wrong, text=None), ModelReply(structured=GOOD, text=None)]
    )
    r = run(b, s)
    assert "the log's ringing line says ARRIVAL" in s.prompts[1]
    assert r.analysis.trigger_path.value == "ARRIVAL" and r.rule_violations == []


def test_an_unrepaired_violation_is_reported_next_to_the_answer():
    b = box()
    wrong = {**GOOD, "trigger_path": "BLIND_HOP"}
    r = run(
        b,
        Scripted(
            b, [ModelReply(structured=wrong, text=None), ModelReply(structured=wrong, text=None)]
        ),
    )
    assert r.analysis.trigger_path.value == "BLIND_HOP"
    assert any("ARRIVAL" in v for v in r.rule_violations)


def facts():
    return facts_for(ride_source().events())


@pytest.mark.parametrize(
    "change,message",
    [
        ({"verdict": "early_false_alarm", "early_late_seconds": 92}, "positive means late"),
        ({"verdict": "structural_success", "trigger_path": "NONE"}, "the log's ringing line says"),
        ({"needs_fix_investigation": True}, "requires suspected_defect"),
        ({"blackout_duration_s": 900}, "matches no location gap"),
        ({"alarm_time": "10:41:30"}, "is not the logged ring"),
        ({"failure_class": "software_defect"}, "not a defect unless a hypothesis"),
    ],
)
def test_rules(change, message):
    violations = check(IncidentAnalysis.model_validate({**GOOD, **change}), facts())
    assert any(message in v for v in violations), violations


def test_the_good_answer_passes_every_rule():
    assert check(IncidentAnalysis.model_validate(GOOD), facts()) == []


def test_the_schema_rejects_an_unknown_verdict():
    with pytest.raises(ValueError):
        IncidentAnalysis.model_validate({**GOOD, "verdict": "probably_fine"})


def test_the_schema_is_a_json_schema_the_sdk_can_take():
    schema = IncidentAnalysis.model_json_schema()
    assert schema["type"] == "object"
    assert {"verdict", "trigger_path", "failure_class"} <= set(schema["required"])


def test_a_gap_that_ends_at_the_ring_counts_as_a_blackout():
    """The last fix, then a backstop ring 500 s later, with no fix in between."""
    from datetime import datetime, timedelta

    from app.agents.source import Ev

    t0 = datetime(2026, 1, 10, 10, 0)
    gps = {"prov": "gps", "acc": "10", "dist": "5000", "src": "GNSS"}
    evs = [
        Ev(1, t0, "DIAG2", "FIX", gps),
        Ev(2, t0 + timedelta(seconds=30), "DIAG2", "FIX", gps),
        Ev(
            3,
            t0 + timedelta(seconds=530),
            "TRIGGER_ALLOWED",
            None,
            {"reason": "TIME_BACKSTOP", "tier": "strong"},
            alarm_id="a",
        ),
        Ev(
            4,
            t0 + timedelta(seconds=530, microseconds=50000),
            "ESCALATION_START",
            None,
            {},
            alarm_id="a",
        ),
    ]
    f = facts_for(evs)
    assert f.longest_gap_s == 500.1 and f.ring_path == "TIME_BACKSTOP_STRONG"
