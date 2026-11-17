from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..db import get_db
from ..schemas import Health

router = APIRouter(tags=["health"])


@router.get(
    "/health",
    response_model=Health,
    summary="Liveness and database reachability",
    responses={503: {"model": Health, "description": "The database is unreachable."}},
)
def health(response: Response, db: Session = Depends(get_db)) -> Health:
    """No auth, so container health checks and uptime monitors can call it.

    Returns 200 only if a `SELECT 1` round-trips to Postgres.
    """
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return Health(status="degraded", database="unreachable")
    return Health(status="ok", database="ok")
