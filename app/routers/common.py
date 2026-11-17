"""Small helpers shared by the routers."""

from datetime import datetime

from fastapi import HTTPException, status


def require_device_local(value: datetime | None, name: str) -> datetime | None:
    """Reject timezone-aware query times.

    Event and window timestamps are the phone's local wall clock with no offset (the log never
    recorded one). Comparing them to a UTC instant would silently shift every result by the
    device's offset, so the API makes the caller say "device-local" explicitly.
    """
    if value is not None and value.tzinfo is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{name} must be a device-local time without a UTC offset, "
            "e.g. 2026-11-06T18:10:00",
        )
    return value
