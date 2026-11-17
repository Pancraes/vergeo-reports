"""Event-line grammar: app/parser/events.py. Pure functions, no database."""

from datetime import datetime
from pathlib import Path

import pytest

from app.parser import MalformedEventLine, parse_event_line
from app.parser.events import BARE_KEY

SAMPLE_LINES = (Path(__file__).parent / "fixtures" / "sample_lines.txt").read_text().splitlines()


def test_normal_event():
    e = parse_event_line(
        "2026-11-01 13:40:13.132|er=275099884|POSITIONING_HEALTH|at=JS_QUERY|risk=HIGH", 7
    )
    assert e is not None
    assert e.line_no == 7
    assert e.device_ts == datetime(2026, 11, 1, 13, 40, 13, 132000)
    assert e.er == 275099884
    assert e.tag == "POSITIONING_HEALTH"
    assert e.subtag is None
    assert e.fields == {"at": "JS_QUERY", "risk": "HIGH"}


def test_subtag_is_the_bare_token_right_after_the_tag():
    e = parse_event_line("2026-10-11 09:16:29.355|er=1|DIAG2|FIX|prov=fused|acc=8.732", 1)
    assert e.tag == "DIAG2"
    assert e.subtag == "FIX"
    assert e.fields == {"prov": "fused", "acc": "8.732"}


def test_duplicate_keys_become_a_list_in_order():
    e = parse_event_line("2026-09-24 17:45:39.848|er=1|BLIND_HOP_CANDIDATE|tier=a|x=1|tier=b", 1)
    assert e.fields == {"tier": ["a", "b"], "x": "1"}


def test_duplicate_key_three_times():
    e = parse_event_line("2026-09-24 17:45:39.848|er=1|T|k=1|k=2|k=3", 1)
    assert e.fields["k"] == ["1", "2", "3"]


def test_empty_value_is_preserved():
    e = parse_event_line("2026-09-24 17:45:39.848|er=1|T|reason=|ef=-", 1)
    assert e.fields == {"reason": "", "ef": "-"}


def test_value_containing_equals_keeps_everything_after_the_first_equals():
    e = parse_event_line("2026-09-24 17:45:39.848|er=1|T|expr=a=b=c", 1)
    assert e.fields == {"expr": "a=b=c"}


def test_value_with_spaces_and_parentheses():
    e = parse_event_line("2026-09-24 17:45:39.848|er=1|T|device=motorola moto g(7) play", 1)
    assert e.fields == {"device": "motorola moto g(7) play"}


def test_bare_token_after_fields_is_kept_not_dropped():
    # Real: AlarmAudioService logs "AUDIO_STOP|source=" + source, and source contains a pipe.
    e = parse_event_line(
        "2026-09-24 18:14:03.881|er=1|AUDIO_STOP|source=STOP_ALL|LOCKSCREEN_HOLD", 1
    )
    assert e.subtag is None
    assert e.fields == {"source": "STOP_ALL", BARE_KEY: ["LOCKSCREEN_HOLD"]}


def test_empty_tokens_are_ignored():
    e = parse_event_line("2026-09-24 18:14:03.881|er=1|T|a=1||b=2", 1)
    assert e.fields == {"a": "1", "b": "2"}


def test_tag_only_line():
    e = parse_event_line("2026-09-24 18:14:03.881|er=1|HEARTBEAT", 1)
    assert e.tag == "HEARTBEAT"
    assert e.fields == {}


@pytest.mark.parametrize(
    "line",
    [
        "APP|ver=1.9.0|code=44|device=x|api=29",
        "ENV|fine=true",
        "==== VERGEO SESSION START 2026-10-02 20:58:41.657 ====",
        "---- recent activity (sanitized, oldest first) ----",
        "",
        "free text the user typed",
    ],
)
def test_non_event_lines_return_none(line):
    assert parse_event_line(line, 1) is None


@pytest.mark.parametrize(
    ("line", "reason_fragment"),
    [
        ("2026-13-45 99:00:00.000|er=1|T|a=1", "invalid timestamp"),
        ("2026-11-01|er=1|T", "invalid timestamp"),
        ("2026-11-01 13:40:13.132|er=abc|T|a=1", "invalid er"),
        ("2026-11-01 13:40:13.132|era=1|T", "expected er="),
        ("2026-11-01 13:40:13.132|er=1|", "invalid or missing tag"),
        ("2026-11-01 13:40:13.132|er=1|lowercase|a=1", "invalid or missing tag"),
        ("2026-11-01 13:40:13.132|er=1", "expected"),
    ],
)
def test_malformed_event_looking_lines_raise(line, reason_fragment):
    with pytest.raises(MalformedEventLine) as exc:
        parse_event_line(line, 42)
    assert exc.value.line_no == 42
    assert reason_fragment in exc.value.reason


# --- real line shapes, synthetic values (tests/fixtures/sample_lines.txt) -------------------


def test_every_sample_line_parses():
    for i, line in enumerate(SAMPLE_LINES, start=1):
        assert parse_event_line(line, i) is not None


def test_sample_positioning_health():
    e = parse_event_line(SAMPLE_LINES[0], 1)
    assert e.tag == "POSITIONING_HEALTH"
    assert e.fields["risk"] == "HIGH"
    assert e.fields["reason"] == "GPS_ONLY_NO_DATA_NO_WIFI"


def test_sample_diag2_fix_subtag():
    e = parse_event_line(SAMPLE_LINES[1], 1)
    assert (e.tag, e.subtag) == ("DIAG2", "FIX")
    assert e.fields["prov"] == "fused"


def test_sample_arrival_is_a_field_not_a_tag():
    e = parse_event_line(SAMPLE_LINES[2], 1)
    assert e.tag == "TRIGGER_ALLOWED"
    assert e.fields["path"] == "ARRIVAL"
    assert e.fields["ef"] == "-"


def test_sample_blind_hop_duplicate_tier():
    e = parse_event_line(SAMPLE_LINES[3], 1)
    assert e.fields["tier"] == ["blind_motion", "blind_motion"]
    assert e.fields["target"] == "[redacted]"


def test_sample_audio_stop_bare_token():
    e = parse_event_line(SAMPLE_LINES[4], 1)
    assert e.fields[BARE_KEY] == ["LOCKSCREEN_HOLD"]


def test_sample_test_session_start_carries_git_sha():
    e = parse_event_line(SAMPLE_LINES[5], 1)
    assert e.fields["gitSha"] == "6358b88"
    assert e.fields["device"] == "motorola moto g(7) play"
