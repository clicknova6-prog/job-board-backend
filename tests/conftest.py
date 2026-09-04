"""Cross-suite test isolation for shared process-level components."""

from collections.abc import Iterator

import pytest
from limits.storage import MemoryStorage
from limits.strategies import FixedWindowRateLimiter

from app.core.filters_cache import filters_cache, filters_cache_invalidator
from app.core.rate_limit import limiter


@pytest.fixture(autouse=True)
def _isolate_rate_limit_storage(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep tests deterministic and independent of a running Redis service."""
    storage = MemoryStorage()
    monkeypatch.setattr(limiter, "_storage", storage)
    monkeypatch.setattr(limiter, "_limiter", FixedWindowRateLimiter(storage))
    yield


class _FakeSyncRedis:
    """Minimal in-memory stand-in for the subset of redis.Redis used here."""

    def __init__(self, data: dict[str, str]) -> None:
        self._data = data

    def get(self, key: str) -> str | None:
        return self._data.get(key)

    def set(
        self, key: str, value: object, *, ex: int | None = None, nx: bool = False
    ) -> bool | None:
        del ex
        if nx and key in self._data:
            return None
        self._data[key] = str(value)
        return True

    def incr(self, key: str) -> int:
        current = int(self._data.get(key, 0)) + 1
        self._data[key] = str(current)
        return current


class _FakeAsyncRedis:
    """Async counterpart of ``_FakeSyncRedis``, backed by the same store."""

    def __init__(self, data: dict[str, str]) -> None:
        self._data = data

    async def get(self, key: str) -> str | None:
        return self._data.get(key)

    async def set(
        self, key: str, value: object, *, ex: int | None = None, nx: bool = False
    ) -> bool | None:
        del ex
        if nx and key in self._data:
            return None
        self._data[key] = str(value)
        return True

    async def incr(self, key: str) -> int:
        current = int(self._data.get(key, 0)) + 1
        self._data[key] = str(current)
        return current


@pytest.fixture(autouse=True)
def _isolate_filters_cache_storage(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep the filters cache deterministic and independent of a running Redis."""
    store: dict[str, str] = {}
    monkeypatch.setattr(filters_cache, "_client", _FakeAsyncRedis(store))
    monkeypatch.setattr(filters_cache_invalidator, "_client", _FakeSyncRedis(store))
    yield
