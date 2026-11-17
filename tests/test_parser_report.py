"""Document structure: app/parser/report.py. Pure functions, no database."""

from datetime import datetime

import pytest

from app.parser import ReportFormatError, parse_report
from app.parser.report import MAX_STORED_WARNINGS, SESSION_START_TAG

from .factory import ALARM, APP, DEFAULT_LINES, build_report


def tags(report):
    return [e.event.tag for e in report.events]


def test_app_header():
    r = parse_report(build_report())
    assert r.app.version == "1.9.0"
    assert r.app.version_code == 44
    assert r.app.debug is False
    assert r.app.device_model == "motorola moto g(7) play"
    assert r.app.android_api == 29


def test_env_line():
    r = parse_report(build_report())
    assert r.env["network"] == "false"
    assert r.env["notif"] == "n/a"
    assert len(r.env) == 11  # the real ENV line has 11 flags


def test_env_dash_means_none():
    assert parse_report(build_report(env="ENV|-")).env is None


def test_description_single_line():
    assert parse_report(build_report(description="Rang late")).description == "Rang late"


def test_multiline_description_is_preserved():
    text = "Rang two stops early.\nExpected the next stop.\n\nThird paragraph"
    r = parse_report(build_report(description=text))
    assert r.description == text


def test_description_line_that_starts_with_env_is_not_mistaken_for_env():
    text = "ENV|looks like env but is line 1\nsecond line"
    r = parse_report(build_report(description=text))
    assert r.description == text
    assert r.env["fine"] == "true"


def test_trailer_parsed_and_line_count_matches():
    r = parse_report(build_report())
    assert r.trailer.diagnostics is True
    assert r.trailer.lines == len(DEFAULT_LINES)
    assert r.trailer.window_truncated is False
    assert r.warning_count == 0


def test_trailer_line_count_mismatch_is_a_warning():
    raw = build_report().replace(b"lines=9 |", b"lines=50 |")
    r = parse_report(raw)
    assert r.trailer.lines == 50
    assert any("trailer says lines=50" in w["reason"] for w in r.warnings)


def test_truncated_report_keeps_events_and_flags_missing_trailer():
    full = build_report()
    cut = full[: full.index(b"BLIND_HOP_CANDIDATE")]  # cut mid-line, like a mail client might
    r = parse_report(cut)
    assert r.trailer is None
    assert "TRIGGER_ALLOWED" in tags(r)
    assert any("truncated" in w["reason"] for w in r.warnings)


def test_diagnostics_omitted():
    r = parse_report(build_report(diagnostics=False))
    assert r.diagnostics_included is False
    assert r.env is None
    assert r.events == []
    assert r.trailer.lines == 0
    assert r.warning_count == 0


def test_events_parsed_in_line_order_with_physical_line_numbers():
    r = parse_report(build_report())
    # Header (4 lines) + ENV + diagnostics header = 6 lines before the first event line.
    assert r.events[0].event.line_no == 7
    line_nos = [e.event.line_no for e in r.events]
    assert line_nos == sorted(line_nos)


def test_session_start_block_becomes_one_event_with_app_and_env_fields():
    r = parse_report(build_report())
    sessions = [e for e in r.events if e.event.tag == SESSION_START_TAG]
    assert len(sessions) == 1
    s = sessions[0].event
    assert s.device_ts == datetime(2026, 11, 6, 17, 44, 38, 1000)
    assert s.er is None
    assert s.fields["ver"] == "1.9.0" and s.fields["network"] == "false"
    # The APP|/ENV| lines are absorbed, not reported as unexpected lines.
    assert r.warning_count == 0


def test_build_sha_propagates_from_test_session_start():
    r = parse_report(build_report())
    by_tag = {e.event.tag: e for e in r.events}
    assert by_tag["POSITIONING_HEALTH"].build_sha is None  # before any TEST_SESSION_START
    assert by_tag["TEST_SESSION_START"].build_sha == "c567346"
    assert by_tag["TRIGGER_ALLOWED"].build_sha == "c567346"
    assert by_tag["AUDIO_STOP"].build_sha == "c567346"


def test_build_sha_resets_at_session_start():
    lines = [
        "2026-11-06 17:44:39.589|er=1|TEST_SESSION_START|alarm=a|gitSha=c567346",
        "2026-11-06 17:45:00.000|er=2|ALARM_EVAL|ID=a",
        "==== VERGEO SESSION START 2026-11-06 18:00:00.000 ====",
        APP,
        "2026-11-06 18:00:01.000|er=3|ALARM_EVAL|ID=a",
        "2026-11-06 18:01:00.000|er=4|TEST_SESSION_START|alarm=a|gitSha=d276341",
        "2026-11-06 18:02:00.000|er=5|ALARM_EVAL|ID=a",
    ]
    r = parse_report(build_report(lines=lines))
    shas = [(e.event.tag, e.build_sha) for e in r.events]
    assert shas == [
        ("TEST_SESSION_START", "c567346"),
        ("ALARM_EVAL", "c567346"),
        (SESSION_START_TAG, None),
        ("ALARM_EVAL", None),  # restarted process: build unknown, not assumed
        ("TEST_SESSION_START", "d276341"),
        ("ALARM_EVAL", "d276341"),
    ]


def test_non_hex_git_sha_is_not_trusted():
    lines = ["2026-11-06 17:44:39.589|er=1|TEST_SESSION_START|gitSha=unknown"]
    r = parse_report(build_report(lines=lines))
    assert r.events[0].build_sha is None


def test_alarm_id_from_id_and_alarm_keys():
    r = parse_report(build_report())
    by_tag = {e.event.tag: e for e in r.events}
    assert by_tag["TRIGGER_ALLOWED"].alarm_id == ALARM  # ID=
    assert by_tag["TEST_SESSION_START"].alarm_id == ALARM  # alarm=
    assert by_tag["POSITIONING_HEALTH"].alarm_id is None


def test_malformed_line_becomes_warning_and_parsing_continues():
    lines = [
        "2026-11-06 17:44:39.589|er=1|ALARM_EVAL|ID=a",
        "2026-11-06 17:44:40.000|er=abc|ALARM_EVAL|ID=a",
        "2026-99-06 17:44:41.000|er=3|ALARM_EVAL|ID=a",
        "2026-11-06 17:44:42.000|er=4|ALARM_EVAL|ID=a",
    ]
    r = parse_report(build_report(lines=lines))
    assert [e.event.er for e in r.events] == [1, 4]
    assert r.warning_count == 2
    assert {w["line_no"] for w in r.warnings} == {8, 9}
    assert "invalid er" in r.warnings[0]["reason"]


def test_unexpected_non_event_line_is_a_warning():
    lines = ["2026-11-06 17:44:39.589|er=1|ALARM_EVAL", "something odd", "APP|stray"]
    r = parse_report(build_report(lines=lines))
    assert len(r.events) == 1
    assert r.warning_count == 2


def test_warnings_are_capped_but_counted():
    lines = ["2026-11-06 17:44:39.589|er=x|T"] * (MAX_STORED_WARNINGS + 50)
    r = parse_report(build_report(lines=lines))
    assert r.warning_count == MAX_STORED_WARNINGS + 50
    assert len(r.warnings) == MAX_STORED_WARNINGS


def test_window_is_min_and_max_device_time_not_first_and_last_line():
    # Wall-clock time can go backwards (DST end, clock correction); line order stays authoritative.
    lines = [
        "2026-11-01 01:50:00.000|er=1|T",
        "2026-11-01 01:05:00.000|er=2|T",  # after the DST fall-back: earlier wall time, later line
    ]
    r = parse_report(build_report(lines=lines))
    assert r.window_start == datetime(2026, 11, 1, 1, 5)
    assert r.window_end == datetime(2026, 11, 1, 1, 50)
    assert [e.event.er for e in r.events] == [1, 2]


def test_crlf_line_endings_are_tolerated():
    r = parse_report(build_report().replace(b"\n", b"\r\n"))
    assert r.app.version == "1.9.0"
    assert r.warning_count == 0


def test_invalid_utf8_is_replaced_and_warned():
    raw = build_report(description="caf\xe9").replace("caf\xe9".encode(), b"caf\xe9")
    r = parse_report(raw)
    assert "�" in r.description
    assert any("UTF-8" in w["reason"] for w in r.warnings)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"", "line 1"),
        (b"hello\n", "line 1"),
        (b"==== VERGEO PROBLEM REPORT ====\nAPP|-\n", "missing ver"),
        (b"==== VERGEO PROBLEM REPORT ====\nnot an app line\n", "APP|"),
        (b"==== VERGEO PROBLEM REPORT ====\nAPP|ver=1|code=x|device=d|api=29\n", "integers"),
        (b"==== VERGEO PROBLEM REPORT ====\nAPP|ver=1|code=44|device=d|api=29\nnope\n", "line 3"),
        (
            b"==== VERGEO PROBLEM REPORT ====\nAPP|ver=1|code=44|device=d|api=29\n"
            b"---- what the user reported ----\nhi\n",
            "missing",
        ),
    ],
)
def test_required_structure_errors(raw, message):
    with pytest.raises(ReportFormatError) as exc:
        parse_report(raw)
    assert message in str(exc.value)
