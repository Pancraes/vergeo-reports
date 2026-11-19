"""Rider lap notes -> timing facts -> phone clock. The notes are real phrasings riders used."""

from datetime import datetime

import pytest

from app.agents.laps import align, parse_laps, resolve_clock

RIDE = (
    "At exactly 10:05 I started laps. 47 seconds later the train started moving. "
    "33 minutes and 10 seconds after that, I got off at Elm Park."
)


def test_natural_language_chain_is_relative_to_the_previous_lap():
    p = parse_laps(RIDE)
    assert [(f.role, f.since_start_s) for f in p.facts] == [
        ("start", 0.0),
        ("train_moving", 47.0),
        ("doors", 2037.0),
    ]
    assert p.facts[0].clock == "10:05"
    assert not any(f.ambiguous for f in p.facts)


def test_bare_plus_offsets_are_read_as_stopwatch_time_and_flagged():
    p = parse_laps("start 11:34:20, ring +21:27 at Cedar, doors +39:54")
    assert [(f.role, f.offset_s, f.relative_to) for f in p.facts[1:]] == [
        ("ring", 1287, "start"),
        ("doors", 2394, "start"),
    ]
    assert all(f.ambiguous for f in p.facts[1:])
    assert any("since the first lap or since the previous" in n for n in p.notes)


def test_minutes_followed_by_a_bare_number_means_seconds():
    p = parse_laps("8:26 is when i turned it on. 4 minutes 40 later it triggered at cedar")
    assert p.facts[1].offset_s == 280
    assert p.facts[1].role == "ring"


def test_a_time_clause_without_event_words_takes_the_next_clause():
    p = parse_laps("1 minute exactly later, i reached birch")
    assert len(p.facts) == 1
    assert p.facts[0].role == "station"
    assert "birch" in p.facts[0].text


@pytest.mark.parametrize(
    "clock,around,expected",
    [
        ("3:05", datetime(2026, 1, 10, 15, 40), datetime(2026, 1, 10, 15, 5)),
        ("3:05", datetime(2026, 1, 10, 2, 0), datetime(2026, 1, 10, 3, 5)),
        ("3:05 pm", datetime(2026, 1, 10, 1, 0), datetime(2026, 1, 10, 15, 5)),
        ("16:34:20", datetime(2026, 1, 11, 17, 0), datetime(2026, 1, 11, 16, 34, 20)),
    ],
)
def test_twelve_hour_clock_lands_nearest_the_ride(clock, around, expected):
    assert resolve_clock(clock, around) == expected


def test_train_moving_lap_is_matched_to_vehicle_motion():
    a = align(
        parse_laps(RIDE),
        around=datetime(2026, 1, 10, 10, 0),
        ring_times=[datetime(2026, 1, 10, 10, 40, 26)],
        vehicle_starts=[datetime(2026, 1, 10, 10, 4, 25), datetime(2026, 1, 10, 10, 5, 45)],
    )
    assert a.method == "moving_lap_vs_vehicle_motion"
    assert a.phone_minus_rider_s == -2.0
    assert a.facts[2]["phone_clock"] == "2026-01-10 10:38:55"


def test_ring_lap_is_the_strongest_anchor():
    a = align(
        parse_laps("start 11:34:20, ring +21:27 at Cedar, doors +39:54"),
        around=datetime(2026, 1, 11, 11, 0),
        ring_times=[datetime(2026, 1, 11, 11, 55, 3)],
        vehicle_starts=[],
    )
    assert a.method == "ring_lap_vs_ESCALATION_START"
    assert a.phone_minus_rider_s == -44.0  # a phone 44 s behind the stopwatch
    assert a.facts[2]["phone_clock"] == "2026-01-11 12:13:30"


def test_without_anchors_rider_time_is_used_and_the_doubt_is_stated():
    a = align(
        parse_laps(RIDE), around=datetime(2026, 1, 10, 10, 0), ring_times=[], vehicle_starts=[]
    )
    assert a.phone_minus_rider_s == 0.0
    assert a.uncertainty_s == 60.0
    assert a.method.startswith("none")


def test_ago_is_placed_from_the_note_time():
    note = (
        "i went on the subway exactly 2 hours and 51 minutes ago, thats when i started the "
        "lap at fare gates, and then 2 minutes and 14 seconds after that, i boarded the train"
    )
    a = align(
        parse_laps(note),
        around=datetime(2026, 1, 12, 23, 0),
        ring_times=[],
        vehicle_starts=[],
        note_written_at=datetime(2026, 1, 13, 1, 48, 30),
    )
    assert [f["phone_clock"] for f in a.facts] == ["2026-01-12 22:57:30", "2026-01-12 22:59:44"]


def test_relative_only_notes_are_unplaceable_not_guessed():
    a = align(
        parse_laps("47 seconds later the train moved"),
        around=datetime(2026, 1, 10, 10),
        ring_times=[],
        vehicle_starts=[],
    )
    assert a.phone_minus_rider_s is None
    assert a.facts[0]["phone_clock"] is None


def test_empty_note_has_no_facts():
    assert parse_laps("it rang late").facts == []
