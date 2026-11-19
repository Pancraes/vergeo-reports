"""Deterministic log readings and the tool wrappers the agent calls."""

import json
from datetime import date, datetime

import pytest

from app.agents import extract as x
from app.agents.corpus import CorpusIndex
from app.agents.privacy import AuditLog
from app.agents.source import Ev
from app.agents.tools import MAX_REPLY_BYTES, ToolBox, ToolSpec
from tests.agents.ride import ride_source


def ev(tag, **fields):
    return Ev(line_no=1, ts=datetime(2026, 1, 10, 10, 0), tag=tag, subtag=None, fields=fields)


@pytest.mark.parametrize(
    "fields,expected",
    [
        ({"path": "ARRIVAL"}, "ARRIVAL"),
        ({"reason": "TIME_BACKSTOP", "tier": "strong"}, "TIME_BACKSTOP_STRONG"),
        ({"reason": "TIME_BACKSTOP", "tier": ["strong", "time_backstop"]}, "TIME_BACKSTOP_STRONG"),
        ({"reason": "TIME_BACKSTOP", "tier": "weak"}, "TIME_BACKSTOP_WEAK"),
        # Blind-hop writes the time-backstop reason; the tier is what separates them.
        ({"reason": "TIME_BACKSTOP", "tier": "blind_motion"}, "BLIND_HOP"),
        ({"reason": "LAST_MILE_SIGNAL_LOSS"}, "NEAR_STOP"),
        ({"reason": "SOMETHING_NEW"}, "OTHER"),
    ],
)
def test_ring_source_reads_the_tier_not_the_shared_reason(fields, expected):
    assert x.ring_source(ev("TRIGGER_ALLOWED", **fields)) == expected


def test_ring_source_without_a_trigger_is_none():
    assert x.ring_source(None) == "NONE"


def test_ride_reading():
    evs = ride_source().events()
    rings = x.find_rings(evs)
    assert [(r["time"], r["trigger_path"]) for r in rings] == [
        ("2026-01-10 10:40:26.877", "ARRIVAL")
    ]
    sessions = x.alarm_sessions(evs)
    assert sessions[-1]["alarm"] == "alarm-a1b2c3"
    assert sessions[-1]["end_reason"] == "stopped_after_ring"
    assert sessions[-1]["settings"]["baseRad"] == "500"

    loc = x.location_summary(evs)
    assert loc["longest_gap"]["from"] == "2026-01-10 10:01:54.806"
    assert loc["longest_gap"]["duration_s"] == pytest.approx(2306.5, abs=0.1)
    assert loc["longest_gap"]["first_fix_after"]["dist"] == 58

    net = x.safety_net(evs)
    assert len(net) == 1 and net[0]["count"] == 3 and net[0]["bh"] == "not_evaluated"
    ml = x.ml_shadow(evs)
    assert ml["predictions"][0]["predicted_crossing"] == "2026-01-10 10:21:40.501"
    assert ml["gated_passes"] == {"stale_or_empty_buffer": 1}
    assert [s["state"] for s in x.motion(evs)["segments"]] == ["vehicle", "walking"]


def test_a_blocked_trigger_without_a_ring_is_reported():
    lines = [
        ev("ALARM_ENABLED"),
        ev("TRIGGER_ALLOWED", path="ARRIVAL", dist="24"),
        ev("TRIGGER_BLOCKED", reason="CREATED_INSIDE_NO_EXIT"),
    ]
    lines = [
        Ev(i, e.ts, e.tag, None, e.fields, alarm_id="f0e1d2c3-0000") for i, e in enumerate(lines)
    ]
    s = x.alarm_sessions(lines)[0]
    assert s["rings"] == []
    assert s["unrung_triggers"][0]["blocked_by"] == "CREATED_INSIDE_NO_EXIT"


def test_network_only_fixes_do_not_hide_a_satellite_blackout():
    rows = [
        {"time": "2026-11-06 18:00:00.000", "src": "GNSS"},
        {"time": "2026-11-06 18:05:00.000", "src": "FUSED_NO_GNSS"},
        {"time": "2026-11-06 18:10:00.000", "src": "FUSED_NO_GNSS"},
        {"time": "2026-11-06 18:20:00.000", "src": "GNSS"},
    ]
    assert max(g["duration_s"] for g in x.gaps(rows, min_gap_s=60)) == 600
    assert x.gaps(rows, min_gap_s=60, gnss_only=True)[0]["duration_s"] == 1200


def test_downsample_keeps_both_ends():
    rows = [{"time": str(i)} for i in range(100)]
    kept, sampled = x.downsample(rows, 30)
    assert sampled and len(kept) == 30
    assert kept[0]["time"] == "0" and kept[-1]["time"] == "99"


def box(**kw) -> ToolBox:
    b = ToolBox(source=ride_source(), corpus=CorpusIndex([]), audit=AuditLog(None), **kw)
    b.default_focus()
    return b


def test_default_focus_is_the_last_alarm_session():
    b = box()
    assert b.focus_from == datetime(2026, 1, 10, 8, 55, 47, 569000)
    assert b.focus_to == datetime(2026, 1, 10, 10, 50, 36, 21000)


def test_tool_call_returns_json_and_counts_rows():
    b = box()
    out = json.loads(b.call("get_rings", {}))
    assert out["rings"][0]["trigger_path"] == "ARRIVAL"
    assert b.calls["get_rings"] == 1
    assert b.events_returned >= 1


def test_tool_errors_come_back_as_json_not_exceptions():
    b = box()
    assert "unknown tool" in json.loads(b.call("drop_table", {}))["error"]
    assert "ValueError" in json.loads(b.call("get_rings", {"from_time": "yesterday"}))["error"]
    assert "TypeError" in json.loads(b.call("get_rings", {"bogus": 1}))["error"]


def test_oversized_replies_are_truncated_with_a_hint():
    b = box()
    b.specs = lambda: [ToolSpec("big", "", {}, lambda: {"x": "y" * 100_000})]
    out = b.call("big", {})
    assert len(out.encode()) < MAX_REPLY_BYTES + 200
    assert "narrow the time window" in out


def test_window_arguments_narrow_every_tool():
    b = box()
    out = json.loads(
        b.call(
            "get_location_fixes",
            {"from_time": "2026-01-10 10:40:00", "to_time": "2026-01-10 10:41:00"},
        )
    )
    assert out["fix_count"] == 2


def test_lap_tools_use_the_logged_anchors():
    out = json.loads(
        box().call(
            "align_laps_to_phone_time",
            {
                "note": (
                    "At exactly 10:05 I started laps. 47 seconds later the train started moving."
                )
            },
        )
    )
    assert out["method"] == "moving_lap_vs_vehicle_motion"
    assert out["phone_minus_rider_s"] == pytest.approx(-1.7, abs=0.1)


CORPUS = """# Field ride corpus
## 2026-11-12: SB U, Cedar false ring
blind-hop rang early at Cedar.
## 2026-11-15 evening: SB U, off_network ARRIVAL at Cedar
off_network ARRIVAL rang early.
### follow-up
still part of 11-15.
## 2026-11-18: the answer to the case under test
late_due_to_complete_blackout.
"""


def test_corpus_hides_sections_on_or_after_the_case_date():
    idx = CorpusIndex.parse(CORPUS)
    assert len(idx.sections) == 3
    assert "follow-up" in idx.sections[1].body
    visible = idx.visible_before(date(2026, 11, 15))
    assert [s.ride_date for s in visible.sections] == [date(2026, 11, 12)]
    assert visible.search("late_due_to_complete_blackout") == []
    assert idx.search("Cedar")[0]["heading"].startswith("2026-11-")


def test_cross_report_tool_says_when_it_is_offline():
    assert "offline" in json.loads(box().call("query_all_reports", {"tag": "X"}))["error"]
