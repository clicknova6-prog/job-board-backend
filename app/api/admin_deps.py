"""Temporary API-key authentication for administrative endpoints."""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Annotated

from dotenv import load_dotenv
from fastapi import Header, HTTPException, status

load_dotenv(Path(__file__).resolve().parents[2] / ".env", encoding="utf-8-sig")


async def require_admin_api_key(
    provided_key: Annotated[str | None, Header(alias="X-Admin-API-Key")] = None,
) -> None:
    """Reject requests that do not carry the temporary administrator API key."""
    expected_key = os.environ.get("ADMIN_API_KEY")
    if (
        not expected_key
        or provided_key is None
        or not secrets.compare_digest(provided_key, expected_key)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing admin API key",
        )
