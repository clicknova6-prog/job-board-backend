"""Infrastructure health/readiness routes for load balancers and uptime monitors."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Annotated

import redis.asyncio as async_redis
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import error_content
from app.core.filters_cache import _prefer_ipv4_loopback
from app.core.rate_limit import limiter
from app.db.async_session import get_async_session

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Health"])

_CHECK_TIMEOUT_SECONDS = 2.0


@router.get("/health")
@limiter.exempt
def liveness() -> dict[str, str]:
    """Report process liveness only -- never checks DB/Redis."""
    return {"status": "ok"}


def _unavailable(check: str) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content=error_content(
            code="SERVICE_UNAVAILABLE",
            message=f"Readiness check failed: {check}",
        ),
    )


@router.get("/health/ready")
@limiter.exempt
async def readiness(
    session: Annotated[AsyncSession, Depends(get_async_session)],
) -> JSONResponse:
    """Report readiness to serve traffic: PostgreSQL and Redis must both respond."""
    try:
        await asyncio.wait_for(
            session.execute(text("SELECT 1")),
            timeout=_CHECK_TIMEOUT_SECONDS,
        )
    except Exception:
        logger.warning("Readiness check failed: database unreachable", exc_info=True)
        return _unavailable("database")

    broker_url = os.environ.get("REDIS_BROKER_URL", "redis://localhost:6379/0")
    redis_client = async_redis.Redis.from_url(
        _prefer_ipv4_loopback(broker_url),
        socket_connect_timeout=0.25,
        socket_timeout=0.25,
    )
    try:
        await asyncio.wait_for(redis_client.ping(), timeout=_CHECK_TIMEOUT_SECONDS)
    except (TimeoutError, RedisError, OSError):
        logger.warning("Readiness check failed: redis unreachable", exc_info=True)
        return _unavailable("redis")
    finally:
        await redis_client.aclose()

    return JSONResponse(status_code=200, content={"status": "ok"})
