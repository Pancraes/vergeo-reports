"""API-key dependencies.

Two keys, both sent as the X-API-Key header:

- ingest key: POST /reports only. This is the key an app build would carry. Anything shipped
  inside an APK can be extracted with apktool in minutes, so this key is abuse friction (stops
  drive-by junk uploads), NOT user authentication. It identifies "some copy of the app", not who.
- admin key: everything that reads or changes stored reports. Only the developer holds it.

The keys are compared with secrets.compare_digest so response timing does not leak how many
leading characters of a guess were right.
"""

import secrets

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader

from .config import Settings, get_settings

_ingest_header = APIKeyHeader(
    name="X-API-Key",
    scheme_name="IngestKey",
    auto_error=False,
    description="Ingest key. Only accepted on POST /reports.",
)
_admin_header = APIKeyHeader(
    name="X-API-Key",
    scheme_name="AdminKey",
    auto_error=False,
    description="Admin key. Required for every read/query/triage endpoint.",
)


def _check(presented: str | None, expected: str) -> None:
    if presented is None or not secrets.compare_digest(presented.encode(), expected.encode()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or invalid X-API-Key",
            headers={"WWW-Authenticate": "ApiKey"},
        )


def require_ingest_key(
    key: str | None = Security(_ingest_header), settings: Settings = Depends(get_settings)
) -> None:
    _check(key, settings.ingest_api_key)


def require_admin_key(
    key: str | None = Security(_admin_header), settings: Settings = Depends(get_settings)
) -> None:
    _check(key, settings.admin_api_key)


# Documents the 401 on routes guarded by require_admin_key (OpenAPI cannot infer it).
ADMIN_KEY_RESPONSES: dict[int | str, dict] = {401: {"description": "Missing or invalid admin key."}}
