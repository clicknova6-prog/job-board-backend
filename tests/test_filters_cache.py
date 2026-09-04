"""Tests for the Redis-backed /api/v1/jobs/filters cache."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, Mock

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app.core.filters_cache import FiltersCache, FiltersCacheInvalidator


def _run(coroutine):
    return asyncio.run(coroutine)


class _StubRedis:
    """In-memory async client isolated from the shared conftest fixture."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def set(
        self, key: str, value: object, *, ex: int | None = None, nx: bool = False
    ) -> bool | None:
        del ex
        if nx and key in self.data:
            return None
        self.data[key] = str(value)
        return True


class _StubSyncRedis:
    """Sync counterpart of ``_StubRedis`` for the invalidator."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.data.get(key)

    def set(
        self, key: str, value: object, *, ex: int | None = None, nx: bool = False
    ) -> bool | None:
        del ex
        if nx and key in self.data:
            return None
        self.data[key] = str(value)
        return True

    def incr(self, key: str) -> int:
        current = int(self.data.get(key, 0)) + 1
        self.data[key] = str(current)
        return current


def test_cache_miss_computes_stores_and_returns_the_result() -> None:
    client = _StubRedis()
    cache = FiltersCache(client, ttl_seconds=3600)
    compute = AsyncMock(
        return_value={"classifications": [{"value": "Eng", "count": 1}]}
    )

    result = _run(cache.get_or_compute(compute))

    assert result == {"classifications": [{"value": "Eng", "count": 1}]}
    compute.assert_awaited_once()
    assert (
        client.data["filters:v1"]
        == '{"classifications": [{"value": "Eng", "count": 1}]}'
    )


def test_cache_hit_returns_without_calling_compute() -> None:
    client = _StubRedis()
    client.data["filters:v1"] = '{"classifications": []}'
    cache = FiltersCache(client, ttl_seconds=3600)
    compute = AsyncMock(
        side_effect=AssertionError("DB query should not run on a cache hit")
    )

    result = _run(cache.get_or_compute(compute))

    assert result == {"classifications": []}
    compute.assert_not_awaited()


def test_version_bump_invalidates_the_previous_versions_cached_data() -> None:
    client = _StubRedis()
    sync_client = _StubSyncRedis()
    # Share one backing store between the async cache and sync invalidator,
    # exactly like the real reader/invalidator pair share one Redis DB.
    sync_client.data = client.data
    cache = FiltersCache(client, ttl_seconds=3600)
    invalidator = FiltersCacheInvalidator(sync_client)

    first_compute = AsyncMock(
        return_value={"classifications": [{"value": "Old", "count": 1}]}
    )
    first = _run(cache.get_or_compute(first_compute))
    assert first == {"classifications": [{"value": "Old", "count": 1}]}

    invalidator.bump_version()

    second_compute = AsyncMock(
        return_value={"classifications": [{"value": "New", "count": 2}]}
    )
    second = _run(cache.get_or_compute(second_compute))

    assert second == {"classifications": [{"value": "New", "count": 2}]}
    second_compute.assert_awaited_once()
    # The stale v1 entry is left in place for its TTL, not deleted.
    assert (
        client.data["filters:v1"]
        == '{"classifications": [{"value": "Old", "count": 1}]}'
    )


def test_first_bump_against_a_cold_cache_still_changes_the_version() -> None:
    """INCR on a missing key would otherwise land on 1, same as the default."""
    sync_client = _StubSyncRedis()
    invalidator = FiltersCacheInvalidator(sync_client)

    invalidator.bump_version()

    assert sync_client.data["filters:version"] == "2"


def test_read_fails_open_when_redis_is_unreachable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = Mock()
    client.get = AsyncMock(side_effect=RedisConnectionError("Redis unavailable"))
    cache = FiltersCache(client, ttl_seconds=3600)
    compute = AsyncMock(return_value={"classifications": []})

    with caplog.at_level(logging.WARNING, logger="app.core.filters_cache"):
        result = _run(cache.get_or_compute(compute))

    assert result == {"classifications": []}
    compute.assert_awaited_once()
    assert "Filters cache unavailable on read; computing directly" in caplog.text


def test_write_is_fail_safe_when_redis_is_unreachable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = Mock()
    client.get = AsyncMock(return_value=None)
    client.set = AsyncMock(side_effect=RedisConnectionError("Redis unavailable"))
    cache = FiltersCache(client, ttl_seconds=3600)
    compute = AsyncMock(return_value={"classifications": []})

    with caplog.at_level(logging.WARNING, logger="app.core.filters_cache"):
        result = _run(cache.get_or_compute(compute))

    assert result == {"classifications": []}
    assert "Failed to write filters cache; continuing uncached" in caplog.text


def test_bump_version_is_fail_safe_when_redis_is_unreachable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = Mock()
    client.set = Mock(side_effect=RedisConnectionError("Redis unavailable"))
    invalidator = FiltersCacheInvalidator(client)

    with caplog.at_level(logging.WARNING, logger="app.core.filters_cache"):
        invalidator.bump_version()

    assert (
        "Failed to bump filters cache version; stale filters may be served"
        in caplog.text
    )
