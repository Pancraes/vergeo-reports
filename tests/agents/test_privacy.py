"""What reaches the model: no destination names, raw alarm ids or positions."""

import json

from app.agents.corpus import CorpusIndex
from app.agents.privacy import AuditLog, sanitize, scrub_text
from app.agents.tools import ToolBox
from tests.agents.ride import ALARM, OTHER, PLACE, ride_source


def test_forbidden_keys_are_dropped_at_any_depth():
    out = sanitize(
        {
            "a": [{"lat": 43.6, "lng": -79.4, "acc": 9}],
            "target": PLACE,
            "nested": {"Destination": "x", "dist": 60},
        }
    )
    assert out == {"a": [{"acc": 9}], "nested": {"dist": 60}}


def test_raw_alarm_ids_become_a_short_hash():
    assert scrub_text(f"ID={ALARM}|x") == "ID=alarm-a1b2c3|x"


def test_position_pairs_are_redacted_but_single_measurements_stay():
    assert scrub_text("at 43.66512, -79.39021 now") == "at [redacted-position] now"
    assert scrub_text("acc=22.512001|spd=1.4") == "acc=22.512001|spd=1.4"


def test_forbidden_key_values_inside_raw_text_are_redacted_up_to_the_pipe():
    # A first version stopped at whitespace and leaked the rest of the name; values may hold spaces.
    assert scrub_text(f"TIME_BACKSTOP_CANDIDATE|target={PLACE}|tier=weak") == (
        "TIME_BACKSTOP_CANDIDATE|target=[redacted]|tier=weak"
    )


def test_no_tool_reply_contains_a_raw_id_or_the_place(tmp_path):
    box = ToolBox(
        source=ride_source(), corpus=CorpusIndex([]), audit=AuditLog(tmp_path / "audit.jsonl")
    )
    box.focus_from, box.focus_to = None, None
    replies = [
        box.call(
            s.name,
            {"tags": ["TIME_BACKSTOP_CANDIDATE"]}
            if s.name == "query_report_events"
            else {"note": "3:05 start"}
            if "laps" in s.name
            else {"query": "x"}
            if s.name == "search_historical_incidents"
            else {"tag": "X"}
            if s.name == "query_all_reports"
            else {},
        )
        for s in box.specs()
    ]
    blob = "\n".join(replies)
    assert ALARM not in blob and OTHER not in blob
    assert PLACE not in blob
    assert "TIME_BACKSTOP_CANDIDATE" in blob  # the line itself was returned, minus the name

    audit = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert len(audit) == len(replies)
    assert sum(a["bytes"] for a in audit) == box.audit.bytes_sent
    assert PLACE not in (tmp_path / "audit.jsonl").read_text()


def test_the_prompt_is_audited_and_scrubbed():
    from app.agents.prompts import triage_task

    assert ALARM not in scrub_text(triage_task("r", f"alarm {ALARM}", None))
