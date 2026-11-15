"""CORS middleware whose allowed origins include every active site's origins.

Starlette's ``CORSMiddleware`` takes a static list. Sites are rows in the DB
and can be added or edited by admins at runtime, so this subclass also allows
the origins of active sites, loaded through ``SiteOriginRegistry`` and
refreshed at most every ``ttl_seconds``. A refresh failure keeps the last
known set (fail-safe: never widens, never crashes a request).

Preflight requests carry no ``X-Site-Key`` value, so CORS itself can only
check "is this the origin of SOME active site". Pairing an origin with its
own site is enforced per request by ``get_current_site``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

OriginLoader = Callable[[], Awaitable[frozenset[str]]]


class SiteOriginRegistry:
    """TTL-cached set of browser origins belonging to active sites."""

    def __init__(
        self,
        loader: OriginLoader,
        *,
        ttl_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._loader = loader
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._origins: frozenset[str] = frozenset()
        self._loaded_at: float | None = None
        self._lock = asyncio.Lock()

    @property
    def origins(self) -> frozenset[str]:
        return self._origins

    def invalidate(self) -> None:
        """Force a reload on the next request (e.g. after an admin site edit)."""
        self._loaded_at = None

    async def refresh_if_stale(self) -> None:
        if not self._is_stale():
            return
        async with self._lock:
            if not self._is_stale():
                return
            try:
                self._origins = await self._loader()
            except Exception:
                logger.warning(
                    "Could not load site CORS origins; keeping last known set",
                    exc_info=True,
                )
            # Stamp even on failure so a DB outage is not hammered per request.
            self._loaded_at = self._clock()

    def _is_stale(self) -> bool:
        return (
            self._loaded_at is None
            or self._clock() - self._loaded_at >= self._ttl_seconds
        )


class SiteAwareCORSMiddleware(CORSMiddleware):
    """``CORSMiddleware`` that also allows origins of active sites."""

    def __init__(self, app: ASGIApp, *, registry: SiteOriginRegistry, **kwargs) -> None:
        super().__init__(app, **kwargs)
        self._registry = registry

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and any(
            name == b"origin" for name, _ in scope.get("headers", ())
        ):
            await self._registry.refresh_if_stale()
        await super().__call__(scope, receive, send)

    def is_allowed_origin(self, origin: str) -> bool:
        return super().is_allowed_origin(origin) or (
            origin.lower() in self._registry.origins
        )


async def load_active_site_origins() -> frozenset[str]:
    """Default loader: read active site origins with a short-lived session."""
    # Imported lazily so importing this module never needs DATABASE_URL.
    from app.db.async_session import AsyncSessionLocal
    from app.db.site_repositories import PublicSiteRepository

    async with AsyncSessionLocal() as session:
        return await PublicSiteRepository(session).list_active_allowed_origins()


site_origin_registry = SiteOriginRegistry(load_active_site_origins)
