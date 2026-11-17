"""Database engine and the per-request session dependency.

Synchronous SQLAlchemy on purpose. FastAPI runs plain `def` endpoints in a threadpool, so a
blocking DB call does not stall the event loop, and the expensive part of ingest (parsing a 2 MB
report) is CPU-bound, which async would not speed up anyway.
"""

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


# pool_pre_ping: a connection that died while idle in the pool is replaced instead of failing the
# next request (matters when Postgres restarts under docker compose).
engine = create_engine(get_settings().database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """One session per request. Closing it returns the connection to the pool and rolls back
    anything a handler left uncommitted."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
