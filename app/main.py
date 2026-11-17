"""FastAPI application: wires the routers together. Run with `uvicorn app.main:app`."""

from fastapi import FastAPI

from .routers import events, health, reports

DESCRIPTION = """
Ingest and query service for **Vergeo problem reports**.

The Vergeo Android app produces a sanitized plain-text report (header, the user's description,
and up to 2 MB of DurableLog diagnostic lines). This service stores each report byte for byte
and parses it into indexed events, so field-test incidents can be queried instead of grepped.

Auth: send `X-API-Key`. The ingest key works only on `POST /reports`; the admin key on
everything else. Times in filters are **device-local without a UTC offset**, because that is
what the log records.
"""

app = FastAPI(
    title="Vergeo Reports API",
    version="0.1.0",
    description=DESCRIPTION,
    openapi_tags=[
        {"name": "reports", "description": "Upload, list, download and triage reports."},
        {"name": "events", "description": "Search parsed diagnostic events across reports."},
        {"name": "health", "description": "Liveness and database reachability."},
    ],
)

app.include_router(health.router)
app.include_router(reports.router)
app.include_router(events.router)
