"""What the model is allowed to see, and a record of everything it was shown.

The Vergeo app sanitizes reports on the device, but the boundary here does not rely on that:

- Older builds logged the destination's name as target= (an August report has a real place name
  there). Newer builds write target=[redacted]. Both are dropped here.
- Every tool reply passes through sanitize() before it is serialized for the model: forbidden
  keys are removed at any depth, raw alarm UUIDs become a short hash, and anything shaped like a
  latitude/longitude pair is redacted.
- AuditLog appends every tool name, its arguments and the exact sanitized bytes sent, so "what did
  the model see" has a file-level answer for every run.

This filters identifiers and positions. It does not make a travel timeline anonymous: times and
destination-relative distances still describe one person's ride, which is why runs stay local
(local_data/ is gitignored) and only the developer's own reports are analysed.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

FORBIDDEN_KEYS = {
    "lat",
    "lng",
    "lon",
    "long",
    "latitude",
    "longitude",
    "latlng",
    "coords",
    "coordinates",
    "target",
    "destination",
    "dest",
    "address",
    "place",
    "placeid",
    "place_id",
    "placename",
    "name",
    "label",
    "title",
    "ssid",
    "bssid",
    "mac",
    "macaddress",
    "email",
    "phone",
    "account",
    "token",
    "apikey",
    "api_key",
    "password",
    "secret",
}
_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
# Two decimal-degree numbers in a row ("43.66512, -79.39021"). Single values like acc=22.512001
# are not positions and stay.
_LATLNG = re.compile(r"-?\d{1,3}\.\d{4,}\s*[,; ]\s*-?\d{1,3}\.\d{4,}")
# Up to the next pipe: log values may contain spaces ("target=<a multi-word place name>").
_KV_IN_TEXT = re.compile(
    r"\b(" + "|".join(sorted(FORBIDDEN_KEYS, key=len, reverse=True)) + r")=([^|\n]*)", re.I
)


def short_uuid(match: re.Match) -> str:
    return "alarm-" + match.group(0).replace("-", "")[:6].lower()


def scrub_text(text: str) -> str:
    text = _UUID.sub(short_uuid, text)
    text = _LATLNG.sub("[redacted-position]", text)
    return _KV_IN_TEXT.sub(lambda m: f"{m.group(1)}=[redacted]", text)


def sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: sanitize(v)
            for k, v in value.items()
            if not (isinstance(k, str) and k.lower() in FORBIDDEN_KEYS)
        }
    if isinstance(value, list | tuple):
        return [sanitize(v) for v in value]
    if isinstance(value, str):
        return scrub_text(value)
    return value


class AuditLog:
    """Append-only JSONL of everything sent to the model in one run."""

    def __init__(self, path: Path | None):
        self.path = path
        self.bytes_sent = 0
        self.entries = 0
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, kind: str, name: str, args: Any, payload: str) -> None:
        self.bytes_sent += len(payload.encode())
        self.entries += 1
        if self.path is None:
            return
        line = {
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "kind": kind,
            "name": name,
            "args": args,
            "bytes": len(payload.encode()),
            "payload": payload,
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
