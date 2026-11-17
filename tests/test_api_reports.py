"""/reports endpoints through the real FastAPI app and Postgres."""

import uuid

import pytest

from .conftest import ADMIN, INGEST
from .factory import build_report


def upload(client, raw=None, report_uuid=None, headers=INGEST):
    data = {"report_uuid": str(report_uuid)} if report_uuid else {}
    return client.post(
        "/reports",
        files={"file": ("problem_report.txt", raw or build_report(), "text/plain")},
        data=data,
        headers=headers,
    )


# --- POST /reports ---------------------------------------------------------------------------


def test_upload_creates_report(client):
    r = upload(client)
    assert r.status_code == 201
    body = r.json()
    assert r.headers["location"] == f"/reports/{body['id']}"
    assert body["app_version"] == "1.9.0"
    assert body["device_model"] == "motorola moto g(7) play"
    assert body["description"] == "Rang late at the destination stop"
    assert body["event_count"] == 7
    assert body["trailer_present"] is True
    assert body["env"]["network"] == "false"
    assert "raw" not in body


def test_identical_retry_returns_200_and_same_report(client):
    key = uuid.uuid4()
    first = upload(client, report_uuid=key)
    second = upload(client, report_uuid=key)
    assert (first.status_code, second.status_code) == (201, 200)
    assert first.json()["id"] == second.json()["id"]


def test_legacy_reupload_without_uuid_is_idempotent(client):
    first = upload(client)
    second = upload(client)
    assert (first.status_code, second.status_code) == (201, 200)
    assert first.json()["report_uuid"] == second.json()["report_uuid"]


def test_uuid_conflict_returns_409_with_existing_id(client):
    key = uuid.uuid4()
    first = upload(client, build_report(description="a"), key)
    r = upload(client, build_report(description="b"), key)
    assert r.status_code == 409
    assert r.json()["detail"]["existing_report_id"] == first.json()["id"]


def test_malformed_header_returns_422(client):
    r = upload(client, b"==== VERGEO PROBLEM REPORT ====\nAPP|-\n")
    assert r.status_code == 422
    assert "missing ver" in r.json()["detail"]


def test_oversized_upload_returns_413(client, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "max_upload_bytes", 1000)
    r = upload(client, build_report() + b"x" * 2000)
    assert r.status_code == 413
    r = client.get("/reports", headers=ADMIN)
    assert r.json()["total"] == 0


def test_invalid_report_uuid_returns_422(client):
    r = client.post(
        "/reports",
        files={"file": ("r.txt", build_report(), "text/plain")},
        data={"report_uuid": "not-a-uuid"},
        headers=INGEST,
    )
    assert r.status_code == 422


def test_missing_file_returns_422(client):
    assert client.post("/reports", headers=INGEST).status_code == 422


# --- GET /reports, /reports/{id}, /raw --------------------------------------------------------


def test_get_report_and_404(client):
    rid = upload(client).json()["id"]
    r = client.get(f"/reports/{rid}", headers=ADMIN)
    assert r.status_code == 200
    assert r.json()["parse_warnings"] == []
    assert client.get(f"/reports/{uuid.uuid4()}", headers=ADMIN).status_code == 404
    assert client.get("/reports/not-a-uuid", headers=ADMIN).status_code == 422


def test_raw_download_is_byte_identical(client):
    raw = build_report(description="byte identical é")
    rid = upload(client, raw).json()["id"]
    r = client.get(f"/reports/{rid}/raw", headers=ADMIN)
    assert r.status_code == 200
    assert r.content == raw
    assert r.headers["content-type"].startswith("text/plain")
    assert "attachment" in r.headers["content-disposition"]
    assert client.get(f"/reports/{uuid.uuid4()}/raw", headers=ADMIN).status_code == 404


def _app(code=44, ver="1.9.0", device="motorola moto g(7) play"):
    return f"APP|ver={ver}|code={code}|debug=false|device={device}|api=29"


def test_list_filters(client):
    upload(client, build_report(description="old", app=_app(code=37, ver="1.8.2")))
    upload(client, build_report(description="new"))
    upload(client, build_report(description="pixel", app=_app(device="Google Pixel 7")))

    def ids(**params):
        r = client.get("/reports", params=params, headers=ADMIN)
        assert r.status_code == 200, r.text
        return sorted(item["description"] for item in r.json()["items"])

    assert ids() == ["new", "old", "pixel"]
    assert ids(app_version="1.8.2") == ["old"]
    assert ids(version_code=44) == ["new", "pixel"]
    assert ids(device="Google Pixel 7") == ["pixel"]
    assert ids(status="new") == ["new", "old", "pixel"]
    assert ids(status="fixed") == []
    assert ids(build_sha="c567346") == ["new", "old", "pixel"]  # all contain that session
    assert ids(build_sha="0000000") == []


def test_list_window_overlap_filter(client):
    early = ["2026-10-01 08:00:00.000|er=1|T", "2026-10-01 09:00:00.000|er=2|T"]
    late = ["2026-11-06 17:00:00.000|er=1|T", "2026-11-06 19:00:00.000|er=2|T"]
    upload(client, build_report(description="early", lines=early))
    upload(client, build_report(description="late", lines=late))

    def ids(**params):
        r = client.get("/reports", params=params, headers=ADMIN)
        return sorted(i["description"] for i in r.json()["items"])

    assert ids(**{"from": "2026-11-06T18:00:00"}) == ["late"]  # overlaps, starts earlier
    assert ids(to="2026-10-01T08:30:00") == ["early"]
    assert ids(**{"from": "2026-10-02T00:00:00", "to": "2026-11-01T00:00:00"}) == []


def test_timezone_aware_filter_is_rejected(client):
    r = client.get("/reports", params={"from": "2026-11-06T18:00:00Z"}, headers=ADMIN)
    assert r.status_code == 422
    assert "device-local" in r.json()["detail"]


def test_offset_pagination(client):
    for i in range(5):
        upload(client, build_report(description=f"r{i}"))
    page1 = client.get("/reports", params={"limit": 2}, headers=ADMIN).json()
    page2 = client.get("/reports", params={"limit": 2, "offset": 2}, headers=ADMIN).json()
    page3 = client.get("/reports", params={"limit": 2, "offset": 4}, headers=ADMIN).json()
    assert page1["total"] == 5
    seen = [i["id"] for p in (page1, page2, page3) for i in p["items"]]
    assert len(seen) == 5 and len(set(seen)) == 5
    assert client.get("/reports", params={"limit": 101}, headers=ADMIN).status_code == 422


# --- PATCH /reports/{id} ---------------------------------------------------------------------


@pytest.fixture
def report_id(client):
    return upload(client).json()["id"]


def patch(client, rid, body):
    return client.patch(f"/reports/{rid}", json=body, headers=ADMIN)


def test_triage_then_fix_with_multiple_commits(client, report_id):
    assert patch(client, report_id, {"status": "triaged"}).json()["status"] == "triaged"
    r = patch(client, report_id, {"status": "fixed", "fix_commits": ["67ddef6", "840BE11"]})
    assert r.status_code == 200
    assert r.json()["status"] == "fixed"
    assert r.json()["fix_commits"] == ["67ddef6", "840be11"]  # normalised to lower case


def test_fixed_requires_a_commit(client, report_id):
    r = patch(client, report_id, {"status": "fixed"})
    assert r.status_code == 422
    r = patch(client, report_id, {"status": "fixed", "fix_commits": []})
    assert r.status_code == 422


def test_fix_commit_must_look_like_a_sha(client, report_id):
    for bad in ["xyz1234", "abc", "a" * 41, "c567346; DROP TABLE"]:
        r = patch(client, report_id, {"status": "fixed", "fix_commits": [bad]})
        assert r.status_code == 422, bad


def test_closed_requires_a_note(client, report_id):
    assert patch(client, report_id, {"status": "closed"}).status_code == 422
    assert (
        patch(client, report_id, {"status": "closed", "resolution_note": "  "}).status_code == 422
    )
    r = patch(client, report_id, {"status": "closed", "resolution_note": "GNSS platform limit"})
    assert r.status_code == 200
    assert r.json()["resolution_note"] == "GNSS platform limit"


def test_cannot_clear_the_note_of_a_closed_report(client, report_id):
    patch(client, report_id, {"status": "closed", "resolution_note": "not a bug"})
    assert patch(client, report_id, {"resolution_note": None}).status_code == 422


def test_cannot_empty_the_commits_of_a_fixed_report(client, report_id):
    patch(client, report_id, {"status": "fixed", "fix_commits": ["c567346"]})
    assert patch(client, report_id, {"fix_commits": []}).status_code == 422


def test_invalid_transitions_return_409(client, report_id):
    patch(client, report_id, {"status": "closed", "resolution_note": "not a bug"})
    r = patch(client, report_id, {"status": "fixed", "fix_commits": ["c567346"]})
    assert r.status_code == 409
    r = patch(client, report_id, {"status": "new"})
    assert r.status_code == 409


def test_reopen_paths(client, report_id):
    patch(client, report_id, {"status": "fixed", "fix_commits": ["a005de9"]})
    r = patch(client, report_id, {"status": "triaged"})  # regression found
    assert r.status_code == 200
    assert r.json()["fix_commits"] == ["a005de9"]  # history kept
    r = patch(client, report_id, {"status": "fixed", "fix_commits": ["a005de9", "3ccb27c"]})
    assert r.json()["fix_commits"] == ["a005de9", "3ccb27c"]


def test_failed_patch_leaves_report_unchanged(client, report_id):
    patch(client, report_id, {"status": "fixed"})
    assert client.get(f"/reports/{report_id}", headers=ADMIN).json()["status"] == "new"


def test_patch_validation(client, report_id):
    assert patch(client, report_id, {}).status_code == 422
    assert patch(client, report_id, {"status": "wontfix"}).status_code == 422
    assert patch(client, report_id, {"unknown": 1}).status_code == 422
    assert patch(client, str(uuid.uuid4()), {"status": "triaged"}).status_code == 404


def test_db_check_constraint_backs_up_the_api(db, report_id):
    """Even bypassing the API, Postgres refuses a fixed report with no commit."""
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        db.execute(text("UPDATE reports SET status='fixed' WHERE id=:id"), {"id": report_id})
        db.commit()
    db.rollback()
    with pytest.raises(IntegrityError):
        db.execute(text("UPDATE reports SET status='bogus' WHERE id=:id"), {"id": report_id})
        db.commit()
