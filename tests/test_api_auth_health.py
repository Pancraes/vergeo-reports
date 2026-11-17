"""Auth boundaries, /health, OpenAPI, and migration drift."""

import uuid

import pytest
from alembic import command
from sqlalchemy.exc import OperationalError

from app.db import get_db
from app.main import app

from .conftest import ADMIN, INGEST, alembic_config
from .factory import build_report

RID = uuid.uuid4()
ADMIN_ROUTES = [
    ("get", "/reports"),
    ("get", f"/reports/{RID}"),
    ("get", f"/reports/{RID}/raw"),
    ("get", f"/reports/{RID}/events"),
    ("patch", f"/reports/{RID}"),
    ("get", "/events"),
]


def post_report(client, headers):
    return client.post(
        "/reports", files={"file": ("r.txt", build_report(), "text/plain")}, headers=headers
    )


@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong"}, ADMIN])
def test_upload_requires_the_ingest_key(client, headers):
    r = post_report(client, headers)
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "ApiKey"


@pytest.mark.parametrize(("method", "path"), ADMIN_ROUTES)
@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong"}, INGEST])
def test_admin_routes_reject_missing_wrong_and_ingest_keys(client, method, path, headers):
    r = client.request(method, path, headers=headers, json={"status": "triaged"})
    assert r.status_code == 401


def test_rejected_upload_stores_nothing(client):
    post_report(client, {"X-API-Key": "wrong"})
    assert client.get("/reports", headers=ADMIN).json()["total"] == 0


def test_health_ok_without_auth(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "database": "ok"}


def test_health_503_when_database_unreachable(client):
    class DeadSession:
        def execute(self, *_):
            raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    app.dependency_overrides[get_db] = lambda: DeadSession()
    try:
        r = client.get("/health")
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 503
    assert r.json()["database"] == "unreachable"


def test_openapi_documents_every_endpoint(client):
    spec = client.get("/openapi.json").json()
    paths = spec["paths"]
    assert set(paths) == {
        "/health",
        "/reports",
        "/reports/{report_id}",
        "/reports/{report_id}/raw",
        "/reports/{report_id}/events",
        "/events",
    }
    assert {"IngestKey", "AdminKey"} <= set(spec["components"]["securitySchemes"])
    assert {"200", "201", "409", "413", "422"} <= set(paths["/reports"]["post"]["responses"])
    assert client.get("/docs").status_code == 200


def test_openapi_documents_the_key_and_401_on_every_guarded_operation(client):
    paths = client.get("/openapi.json").json()["paths"]
    operations = [(p, m, op) for p, ops in paths.items() for m, op in ops.items() if p != "/health"]
    assert len(operations) == 7  # plus /health: the 8 endpoints
    for path, method, op in operations:
        expected = "IngestKey" if (path, method) == ("/reports", "post") else "AdminKey"
        assert op["security"] == [{expected: []}], (path, method)
        assert "401" in op["responses"], (path, method)
    assert "security" not in paths["/health"]["get"]


def test_models_and_migrations_have_not_drifted():
    """Fails if app/models.py changed without a new Alembic migration."""
    command.check(alembic_config())
