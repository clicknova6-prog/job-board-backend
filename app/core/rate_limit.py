"""Shared API rate limiter configuration."""

from fastapi import Request
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app.core.errors import error_content

limiter = Limiter(key_func=get_remote_address)


def rate_limit_exceeded_handler(
    request: Request,
    exc: RateLimitExceeded,
) -> JSONResponse:
    """Return the global error envelope while preserving SlowAPI headers."""
    response = JSONResponse(
        status_code=429,
        content=error_content(
            code="RATE_LIMITED",
            message=f"Rate limit exceeded: {exc.detail}",
        ),
    )
    return request.app.state.limiter._inject_headers(
        response,
        request.state.view_rate_limit,
    )
