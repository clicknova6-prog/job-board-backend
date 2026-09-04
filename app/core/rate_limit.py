"""Shared API rate limiter configuration."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded

from app.api.request_helpers import get_client_ip
from app.core.config import RateLimitSettings
from app.core.errors import error_content

logger = logging.getLogger(__name__)


def _client_ip_key(request: Request) -> str:
    return get_client_ip(request) or "unknown"


class FailOpenLimiter(Limiter):
    """SlowAPI limiter that allows requests when Redis cannot be reached."""

    def _check_request_limit(
        self,
        request: Request,
        endpoint_func: Callable[..., Any] | None,
        in_middleware: bool = True,
    ) -> None:
        try:
            super()._check_request_limit(request, endpoint_func, in_middleware)
        except RateLimitExceeded:
            raise
        except Exception:
            request.state.view_rate_limit = None
            logger.warning(
                "Rate limit Redis storage unavailable; allowing request",
                exc_info=True,
            )


def create_limiter(
    settings: RateLimitSettings,
    *,
    storage_uri: str | None = None,
) -> FailOpenLimiter:
    """Create the shared limiter, with an override for isolated tests."""
    return FailOpenLimiter(
        key_func=_client_ip_key,
        storage_uri=storage_uri or settings.storage_uri,
        key_prefix="job_board",
        storage_options={
            "socket_connect_timeout": 0.25,
            "socket_timeout": 0.25,
        },
    )


rate_limit_settings = RateLimitSettings.from_environment()
limiter = create_limiter(rate_limit_settings)


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
        headers={"Retry-After": str(max(1, exc.limit.limit.get_expiry()))},
    )
    return response
