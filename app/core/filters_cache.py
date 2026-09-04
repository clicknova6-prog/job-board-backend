"""Redis-backed cache for the /api/v1/jobs/filters response.

The cache key is versioned ("filters:v{version}"), where the version is a
plain integer stored under "filters:version". Reads fetch the current
version then that key; a hit skips the DB query entirely, a miss computes,
stores, and returns. Invalidation never deletes a key -- it bumps the
version so the next read misses and recomputes under a new key, while old
keys expire naturally via TTL.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import redis
import redis.asyncio
from redis.exceptions import RedisError

from app.core.config import FiltersCacheSettings

logger = logging.getLogger(__name__)

_VERSION_KEY = "filters:version"


def _cache_key(version: int) -> str:
    return f"filters:v{version}"


def _prefer_ipv4_loopback(storage_uri: str) -> str:
    """Rewrite a "localhost" host to "127.0.0.1".

    asyncio's ProactorEventLoop (the default on Windows) can take seconds to
    resolve "localhost" via getaddrinfo before falling back to IPv4, which
    blows straight through the short connect timeout used to fail open
    quickly on a genuinely unreachable Redis. An IP literal skips DNS
    resolution entirely, so this only ever helps, never hides real outages.
    """
    parts = urlsplit(storage_uri)
    if parts.hostname != "localhost":
        return storage_uri
    netloc = "127.0.0.1"
    if parts.port is not None:
        netloc = f"{netloc}:{parts.port}"
    if parts.username is not None:
        credentials = parts.username
        if parts.password is not None:
            credentials = f"{credentials}:{parts.password}"
        netloc = f"{credentials}@{netloc}"
    return urlunsplit(parts._replace(netloc=netloc))


def create_async_filters_cache_client(
    settings: FiltersCacheSettings,
    *,
    storage_uri: str | None = None,
) -> redis.asyncio.Redis:
    """Create the async Redis client used by the API read path."""
    return redis.asyncio.Redis.from_url(
        _prefer_ipv4_loopback(storage_uri or settings.storage_uri),
        socket_connect_timeout=0.25,
        socket_timeout=0.25,
    )


def create_sync_filters_cache_client(
    settings: FiltersCacheSettings,
    *,
    storage_uri: str | None = None,
) -> redis.Redis:
    """Create the sync Redis client used by the import promotion path."""
    return redis.Redis.from_url(
        storage_uri or settings.storage_uri,
        socket_connect_timeout=0.25,
        socket_timeout=0.25,
    )


class FiltersCache:
    """Async read-through cache for the filters response (API side)."""

    def __init__(
        self,
        client: redis.asyncio.Redis,
        ttl_seconds: int,
        *,
        service_logger: logging.Logger | None = None,
    ) -> None:
        self._client = client
        self._ttl_seconds = ttl_seconds
        self.logger = service_logger or logger

    async def get_or_compute(
        self,
        compute: Callable[[], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        """Return the cached filters payload, computing and storing on a miss.

        Falls open to `compute()` uncached if Redis cannot be reached.
        """
        try:
            version = await self._current_version()
            cached = await self._client.get(_cache_key(version))
        except RedisError:
            self.logger.warning(
                "Filters cache unavailable on read; computing directly",
                exc_info=True,
            )
            return await compute()

        if cached is not None:
            return json.loads(cached)

        result = await compute()
        await self._store(version, result)
        return result

    async def _current_version(self) -> int:
        raw = await self._client.get(_VERSION_KEY)
        return int(raw) if raw is not None else 1

    async def _store(self, version: int, data: dict[str, Any]) -> None:
        try:
            await self._client.set(
                _cache_key(version),
                json.dumps(data),
                ex=self._ttl_seconds,
            )
        except RedisError:
            self.logger.warning(
                "Failed to write filters cache; continuing uncached",
                exc_info=True,
            )


class FiltersCacheInvalidator:
    """Sync version-bump invalidation for the filters cache (import side)."""

    def __init__(
        self,
        client: redis.Redis,
        *,
        service_logger: logging.Logger | None = None,
    ) -> None:
        self._client = client
        self.logger = service_logger or logger

    def bump_version(self) -> None:
        """Advance the filters cache version so the next read recomputes.

        Seeds the version key to 1 first (no-op if already set) so the very
        first bump against a cold cache still lands on 2, not 1 -- otherwise
        Redis's INCR-on-missing-key semantics (0 -> 1) would leave the
        version unchanged from the implicit default readers already assume.
        """
        try:
            self._client.set(_VERSION_KEY, 1, nx=True)
            self._client.incr(_VERSION_KEY)
        except RedisError:
            self.logger.warning(
                "Failed to bump filters cache version; stale filters may be served",
                exc_info=True,
            )


filters_cache_settings = FiltersCacheSettings.from_environment()
filters_cache = FiltersCache(
    create_async_filters_cache_client(filters_cache_settings),
    filters_cache_settings.ttl_seconds,
)
filters_cache_invalidator = FiltersCacheInvalidator(
    create_sync_filters_cache_client(filters_cache_settings)
)
