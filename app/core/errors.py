"""Shared construction helpers for API error responses."""

import re
from http import HTTPStatus
from typing import Any

ERROR_CODES = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    409: "CONFLICT",
    422: "VALIDATION_ERROR",
    429: "RATE_LIMITED",
    500: "INTERNAL_ERROR",
    503: "SERVICE_UNAVAILABLE",
}


def error_code(status_code: int) -> str:
    """Return a stable uppercase snake-case code for an HTTP status."""
    if status_code in ERROR_CODES:
        return ERROR_CODES[status_code]
    try:
        phrase = HTTPStatus(status_code).phrase
    except ValueError:
        return f"HTTP_{status_code}_ERROR"
    return re.sub(r"[^A-Z0-9]+", "_", phrase.upper()).strip("_")


def error_content(
    *,
    code: str,
    message: str,
    details: Any = None,
) -> dict[str, dict[str, Any]]:
    """Build the global API error envelope."""
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details,
        }
    }
