"""/reports/{id}/events and /events through the real app and Postgres (JSONB, GIN, keyset)."""

import uuid

from .conftest import ADMIN, INGEST
from .factory import ALARM, build_report


def upload(client, raw):
    r = client.post("/reports", files={"file": ("r.txt", raw, "text/plain")}, headers=INGEST)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def get(client, path, **params):
    r = client.get(path, params=params, headers=ADMIN)
    assert r.status_code == 200, r.text
    return r.json()


# --- per-report timeline ---------------------------------------------------------------------


def test_report_events_in_line_order(client):
    rid = upload(client, build_report())
    page = get(client, f"/reports/{rid}/events")
    lines = [e["line_no"] for e in page["items"]]
    assert lines == sorted(lines)
    assert page["next_cursor"] is None
    tags = [e["tag"] for e in page["items"]]
    assert tags[0] == "POSITIONING_HEALTH" and "SESSION_START" in tags


def test_report_events_tag_filter_repeatable(client):
    rid = upload(client, build_report())
    page = get(client, f"/reports/{rid}/events", tag=["TRIGGER_ALLOWED", "AUDIO_STOP"])
    assert sorted(e["tag"] for e in page["items"]) == ["AUDIO_STOP", "TRIGGER_ALLOWED"]


def test_report_events_time_range_is_half_open(client):
    rid = upload(client, build_report())
    page = get(
        client,
        f"/reports/{rid}/events",
        **{"from": "2026-11-06T18:13:57.718", "to": "2026-11-06T18:14:04"},
    )
    assert [e["tag"] for e in page["items"]] == ["TRIGGER_ALLOWED", "BLIND_HOP_CANDIDATE"]


def test_report_events_alarm_and_subtag(client):
    rid = upload(client, build_report())
    assert len(get(client, f"/reports/{rid}/events", alarm_id=ALARM)["items"]) == 3
    page = get(client, f"/reports/{rid}/events", subtag="FIX")
    assert [(e["tag"], e["subtag"]) for e in page["items"]] == [("DIAG2", "FIX")]


def test_report_events_cursor_walks_every_event_once(client):
    lines = [f"2026-11-06 17:00:{i:02d}.000|er={i}|T|i={i}" for i in range(25)]
    rid = upload(client, build_report(lines=lines))
    seen, cursor = [], None
    while True:
        params = {"limit": 10, **({"cursor": cursor} if cursor else {})}
        page = get(client, f"/reports/{rid}/events", **params)
        seen += [e["fields"]["i"] for e in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == [str(i) for i in range(25)]


def test_report_events_404_and_bad_inputs(client):
    rid = upload(client, build_report())
    r = client.get(f"/reports/{uuid.uuid4()}/events", headers=ADMIN)
    assert r.status_code == 404
    r = client.get(f"/reports/{rid}/events", params={"cursor": "%%%"}, headers=ADMIN)
    assert r.status_code == 422
    r = client.get(f"/reports/{rid}/events", params={"field": "noColon"}, headers=ADMIN)
    assert r.status_code == 422


# --- cross-report search ---------------------------------------------------------------------


def two_builds(client):
    """Two reports from two builds, each with one ARRIVAL and one TIME_BACKSTOP trigger."""

    def lines(sha, day):
        return [
            f"2026-11-{day} 17:44:39.589|er=1|TEST_SESSION_START|alarm={ALARM}|gitSha={sha}",
            f"2026-11-{day} 18:13:57.718|er=2|TRIGGER_ALLOWED|ID={ALARM}|path=ARRIVAL|dist=1982",
            f"2026-11-{day} 18:51:59.902|er=3|TRIGGER_ALLOWED|reason=TIME_BACKSTOP|ID={ALARM}"
            "|tier=weak|tier=weak",
            f"2026-11-{day} 18:52:00.000|er=4|POSITIONING_HEALTH|risk=HIGH",
        ]

    a = upload(client, build_report(description="a", lines=lines("1473d6d", "06")))
    b = upload(client, build_report(description="b", lines=lines("c567346", "15")))
    return a, b


def test_every_arrival_across_reports(client):
    two_builds(client)
    page = get(client, "/events", field="path:ARRIVAL")
    assert len(page["items"]) == 2
    assert {e["tag"] for e in page["items"]} == {"TRIGGER_ALLOWED"}


def test_every_arrival_in_one_build(client):
    a, b = two_builds(client)
    page = get(client, "/events", field="path:ARRIVAL", build_sha="c567346")
    assert [e["report_id"] for e in page["items"]] == [b]


def test_tag_plus_field_filter(client):
    two_builds(client)
    page = get(client, "/events", tag="TRIGGER_ALLOWED", field="reason:TIME_BACKSTOP")
    assert len(page["items"]) == 2


def test_field_filter_matches_duplicate_key_lists(client):
    """Regression: {"tier": ["weak","weak"]} @> {"tier": "weak"} is false in Postgres, so the
    filter must also try the list form or duplicate-key events vanish from search."""
    two_builds(client)
    page = get(client, "/events", field="tier:weak")
    assert len(page["items"]) == 2


def test_multiple_field_filters_are_anded(client):
    two_builds(client)
    assert len(get(client, "/events", field=["path:ARRIVAL", "dist:1982"])["items"]) == 2
    assert get(client, "/events", field=["path:ARRIVAL", "dist:9999"])["items"] == []


def test_field_value_may_contain_colons_and_is_exact(client):
    lines = ["2026-11-06 17:00:00.000|er=1|T|at=12:30:00", "2026-11-06 17:00:01.000|er=2|T|at=12"]
    upload(client, build_report(lines=lines))
    assert len(get(client, "/events", field="at:12:30:00")["items"]) == 1
    assert len(get(client, "/events", field="at:12")["items"]) == 1


def test_field_key_is_validated(client):
    for bad in ['path":"x', "a b:c", ":ARRIVAL", "1abc:x"]:
        r = client.get("/events", params={"field": bad}, headers=ADMIN)
        assert r.status_code == 422, bad


def test_filters_through_report_columns(client):
    two_builds(client)
    assert len(get(client, "/events", tag="POSITIONING_HEALTH", app_version="1.9.0")["items"]) == 2
    assert get(client, "/events", device="Google Pixel 7")["items"] == []
    assert len(get(client, "/events", version_code=44, field="path:ARRIVAL")["items"]) == 2


def test_cross_report_time_range_and_alarm(client):
    two_builds(client)
    page = get(client, "/events", alarm_id=ALARM, **{"from": "2026-11-10T00:00:00"})
    assert len(page["items"]) == 3  # report b only: session start + two triggers


def test_global_cursor_pagination_with_identical_timestamps(client):
    """Overlapping reports repeat lines with identical device_ts; the id tie-breaker must still
    return every row exactly once."""
    lines = [f"2026-11-06 17:00:00.000|er={i}|T|i={i}" for i in range(7)]  # all same timestamp
    upload(client, build_report(description="x", lines=lines))
    upload(client, build_report(description="y", lines=lines))
    seen, cursor = [], None
    while True:
        params = {"tag": "T", "limit": 3, **({"cursor": cursor} if cursor else {})}
        page = get(client, "/events", **params)
        seen += [e["id"] for e in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == 14 and len(set(seen)) == 14


def test_global_cursor_rejects_garbage(client):
    from app.services.queries import encode_cursor

    for bad in ["???", encode_cursor({"nope": 1}), encode_cursor({"ts": "x", "id": 1})]:
        r = client.get("/events", params={"cursor": bad}, headers=ADMIN)
        assert r.status_code == 422, bad


def test_events_limit_bounds(client):
    assert client.get("/events", params={"limit": 0}, headers=ADMIN).status_code == 422
    assert client.get("/events", params={"limit": 1001}, headers=ADMIN).status_code == 422
