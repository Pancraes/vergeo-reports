"""The evaluation scorer: what counts as a match, and that failures are kept."""

from app.agents.evaluation import Case, agent_direction, score, summarize
from app.agents.schemas import IncidentAnalysis, RunMetrics, TriageResult

CASE = Case.from_json(
    {
        "id": "synthetic-1",
        "report": "x.txt",
        "from": "2026-01-10 09:50:00",
        "to": "2026-01-10 10:42:00",
        "ride_date": "2026-01-10",
        "notes": None,
        "label_source": "test",
        "expected": {
            "verdicts": ["late_due_to_complete_blackout"],
            "trigger_path": "ARRIVAL",
            "direction": "late",
            "blackout": True,
            "failure_class": ["known_limit"],
            "doors_phone": "2026-01-10 10:38:55",
            "doors_uncertainty_s": 15,
            "early_late_s": 92,
        },
    }
)
BASE = {
    "report_id": "r",
    "verdict": "late_due_to_complete_blackout",
    "confidence": "high",
    "failure_class": "known_limit",
    "trigger_path": "ARRIVAL",
    "blackout_duration_s": 2306,
    "estimated_arrival_or_doors_time": "10:39:10",
    "early_late_seconds": 77,
}
METRICS = RunMetrics(wall_seconds=60, tool_calls=12, cost_usd=0.4)


def result(**change):
    return TriageResult(
        analysis=IncidentAnalysis.model_validate({**BASE, **change}), metrics=METRICS
    )


def test_case_from_json_maps_from():
    assert CASE.from_ == "2026-01-10 09:50:00"


def test_a_matching_answer_scores_on_every_axis():
    row = score(CASE, result())
    assert all(
        row[k] for k in ("verdict", "trigger_path", "direction", "blackout", "failure_class")
    )
    assert row["doors_error_s"] == 15.0
    assert row["early_late_error_s"] == 15.0


def test_calling_a_zero_signal_late_ride_a_bug_is_scored_wrong():
    row = score(
        CASE,
        result(
            verdict="software_defect",
            failure_class="software_defect",
            suspected_defect=True,
            defect_hypothesis="x",
        ),
    )
    assert row["verdict"] is False and row["failure_class"] is False


def test_direction_comes_from_the_verdict_then_the_seconds():
    a = IncidentAnalysis.model_validate(
        {**BASE, "verdict": "structural_success", "early_late_seconds": -400}
    )
    assert agent_direction(a) == "early"
    assert (
        agent_direction(IncidentAnalysis.model_validate({**BASE, "trigger_path": "NONE"}))
        == "no_ring"
    )


def test_failed_runs_are_kept_and_count_against_accuracy():
    failed = TriageResult(analysis=None, metrics=METRICS, error="no analysis returned")
    rows = [score(CASE, result()), score(CASE, failed)]
    s = summarize(rows)
    assert s["cases"] == 2 and s["completed"] == 1
    assert s["verdict"] == {"correct": 1, "scored": 2}
    assert s["blackout"] == {"correct": 1, "scored": 1}  # unscored for the failed run
    assert s["wall_seconds"]["median"] == 60
