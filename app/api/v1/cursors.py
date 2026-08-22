"""Opaque base64 keyset cursors for the public listing endpoints.

The payload is deliberately minimal -- the sort position and nothing else --
so a cursor cannot be used to smuggle filter or authorization state. It is
encoded, not signed: callers can decode it, and that is acceptable because it
only ever names a page boundary of already-public data.
"""

from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime

from app.db.public_job_repositories import JobKeysetCursor


class InvalidCursorError(ValueError):
    """Raised when a client-supplied cursor cannot be decoded."""


def encode_cursor(cursor: JobKeysetCursor) -> str:
    """Serialize a keyset position into a URL-safe base64 token."""
    payload = json.dumps(
        {"v": cursor.last_imported_at.isoformat(), "id": cursor.id},
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def decode_cursor(token: str) -> JobKeysetCursor:
    """Parse a cursor token, raising :class:`InvalidCursorError` if malformed."""
    try:
        # validate=False tolerates missing padding only via the explicit pad
        # below; anything else non-base64 still raises.
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        payload = json.loads(raw.decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise InvalidCursorError("cursor is not valid base64-encoded JSON") from exc

    if not isinstance(payload, dict):
        raise InvalidCursorError("cursor payload must be a JSON object")

    raw_timestamp = payload.get("v")
    raw_id = payload.get("id")
    if not isinstance(raw_timestamp, str):
        raise InvalidCursorError("cursor field 'v' must be an ISO-8601 string")
    # bool is a subclass of int, so it is rejected explicitly.
    if not isinstance(raw_id, int) or isinstance(raw_id, bool):
        raise InvalidCursorError("cursor field 'id' must be an integer")

    try:
        last_imported_at = datetime.fromisoformat(raw_timestamp)
    except ValueError as exc:
        raise InvalidCursorError("cursor field 'v' is not a valid timestamp") from exc

    return JobKeysetCursor(last_imported_at=last_imported_at, id=raw_id)
