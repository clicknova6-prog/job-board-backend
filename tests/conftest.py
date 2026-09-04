"""Cross-suite test isolation for shared process-level components."""

from collections.abc import Iterator

import pytest
from limits.storage import MemoryStorage
from limits.strategies import FixedWindowRateLimiter

from app.core.rate_limit import limiter


@pytest.fixture(autouse=True)
def _isolate_rate_limit_storage(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep tests deterministic and independent of a running Redis service."""
    storage = MemoryStorage()
    monkeypatch.setattr(limiter, "_storage", storage)
    monkeypatch.setattr(limiter, "_limiter", FixedWindowRateLimiter(storage))
    yield
