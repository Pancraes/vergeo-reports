"""Test setup: a real PostgreSQL database, migrated with Alembic exactly like production.

Point TEST_DATABASE_URL at a disposable database (CI uses a Postgres service container). The
schema is dropped and rebuilt with `alembic upgrade head` once per test run, and every test starts
with empty tables.
"""

import os

from sqlalchemy.engine import make_url

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://vergeo:vergeo@localhost:5433/vergeo_test"
)
# Must happen before anything imports app.config / app.db.
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ["INGEST_API_KEY"] = "test-ingest-key"
os.environ["ADMIN_API_KEY"] = "test-admin-key"
os.environ["MAX_UPLOAD_BYTES"] = str(4 * 1024 * 1024)

import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.db import SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402

INGEST = {"X-API-Key": "test-ingest-key"}
ADMIN = {"X-API-Key": "test-admin-key"}


def alembic_config() -> Config:
    cfg = Config(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    return cfg


@pytest.fixture(scope="session", autouse=True)
def migrated_database():
    # Guard: this fixture wipes the schema, so refuse anything that is not obviously a test DB.
    if "test" not in (make_url(TEST_DATABASE_URL).database or ""):
        raise RuntimeError(f"refusing to reset non-test database {TEST_DATABASE_URL!r}")
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    command.upgrade(alembic_config(), "head")
    yield


@pytest.fixture(autouse=True)
def clean_tables():
    yield
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE events, reports RESTART IDENTITY CASCADE"))


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c
