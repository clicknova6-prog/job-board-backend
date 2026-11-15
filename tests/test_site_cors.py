"""Unit tests for the TTL-cached site CORS origin registry."""

from __future__ import annotations

import asyncio

from app.core.site_cors import SiteOriginRegistry
from app.db.site_repositories import origin_of, site_allowed_origins


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_registry_reloads_only_after_ttl() -> None:
    calls: list[int] = []

    async def loader() -> frozenset[str]:
        calls.append(1)
        return frozenset({f"https://site{len(calls)}.test"})

    clock = _Clock()
    registry = SiteOriginRegistry(loader, ttl_seconds=60, clock=clock)

    async def run() -> None:
        await registry.refresh_if_stale()
        await registry.refresh_if_stale()
        assert registry.origins == {"https://site1.test"}
        clock.now = 61
        await registry.refresh_if_stale()
        assert registry.origins == {"https://site2.test"}
        registry.invalidate()
        await registry.refresh_if_stale()

    asyncio.run(run())
    assert len(calls) == 3


def test_registry_keeps_last_known_origins_when_loading_fails() -> None:
    state = {"fail": False}

    async def loader() -> frozenset[str]:
        if state["fail"]:
            raise RuntimeError("db down")
        return frozenset({"https://ok.test"})

    clock = _Clock()
    registry = SiteOriginRegistry(loader, ttl_seconds=10, clock=clock)

    async def run() -> None:
        await registry.refresh_if_stale()
        state["fail"] = True
        clock.now = 11
        await registry.refresh_if_stale()

    asyncio.run(run())
    assert registry.origins == {"https://ok.test"}


def test_origin_helpers_normalize_and_skip_invalid_values() -> None:
    assert origin_of("https://Example.com/jobs?x=1") == "https://example.com"
    assert origin_of("http://localhost:3000/") == "http://localhost:3000"
    assert origin_of("example.com") is None
    assert origin_of("ftp://example.com") is None
    assert site_allowed_origins(
        "https://it.test/", {"cors_origins": ["https://www.it.test", "bad", 7]}
    ) == {"https://it.test", "https://www.it.test"}
