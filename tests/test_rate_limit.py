"""Tests for shared Redis-backed API rate limiting."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisConnectionError
from slowapi.errors import RateLimitExceeded

from app.core.config import RateLimitSettings
from app.core.rate_limit import (
    FailOpenLimiter,
    create_limiter,
    limiter,
    rate_limit_exceeded_handler,
)
from app.main import app


def _test_app(test_limiter: FailOpenLimiter, limit: str = "1/minute") -> FastAPI:
    test_app = FastAPI()
    test_app.state.limiter = test_limiter
    test_app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)

    @test_app.get("/limited")
    @test_limiter.limit(limit)
    async def limited(request: Request) -> dict[str, bool]:
        return {"ok": True}

    return test_app


def test_limit_enforcement_uses_forwarded_client_ip() -> None:
    settings = RateLimitSettings.from_environment()
    test_limiter = create_limiter(settings, storage_uri="memory://")
    client = TestClient(_test_app(test_limiter))

    first = client.get("/limited", headers={"X-Forwarded-For": "203.0.113.10"})
    limited = client.get(
        "/limited",
        headers={"X-Forwarded-For": "203.0.113.10, 10.0.0.1"},
    )
    other_client = client.get(
        "/limited",
        headers={"X-Forwarded-For": "203.0.113.11"},
    )

    assert first.status_code == 200
    assert limited.status_code == 429
    assert limited.json() == {
        "error": {
            "code": "RATE_LIMITED",
            "message": "Rate limit exceeded: 1 per 1 minute",
            "details": None,
        }
    }
    assert limited.headers["Retry-After"] == "60"
    assert other_client.status_code == 200


def test_redis_failure_is_logged_and_fails_open(
    caplog: pytest.LogCaptureFixture,
) -> None:
    redis_client = Mock()
    redis_client.register_script.return_value = Mock(
        side_effect=RedisConnectionError("Redis unavailable")
    )

    with patch("redis.from_url", return_value=redis_client):
        test_limiter = create_limiter(RateLimitSettings.from_environment())

    with caplog.at_level(logging.WARNING, logger="app.core.rate_limit"):
        response = TestClient(_test_app(test_limiter)).get("/limited")

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert "Redis storage unavailable; allowing request" in caplog.text


def test_crawler_and_liveness_routes_never_touch_rate_limit_storage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sitemap_index = tmp_path / "sitemap.xml"
    sitemap_chunk = tmp_path / "sitemap-1.xml.gz"
    sitemap_index.write_text("<sitemapindex />", encoding="utf-8")
    sitemap_chunk.write_bytes(b"compressed sitemap")
    monkeypatch.setenv("PUBLIC_SITE_BASE_URL", "https://jobs.example")
    monkeypatch.setenv("SITEMAP_OUTPUT_DIR", str(tmp_path))

    redis_increment = Mock(side_effect=AssertionError("rate limit counter touched"))
    monkeypatch.setattr(limiter._storage, "incr", redis_increment)
    client = TestClient(app)

    responses = [
        client.get("/"),
        client.get("/robots.txt"),
        client.get("/sitemap.xml"),
        client.get("/sitemap-1.xml.gz"),
    ]

    assert [response.status_code for response in responses] == [200, 200, 200, 200]
    redis_increment.assert_not_called()


def test_rate_limit_redis_db_is_distinct_from_broker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "REDIS_BROKER_URL",
        "rediss://user:secret@redis.example:6380/4?socket_timeout=1&db=4",
    )
    monkeypatch.setenv("REDIS_RATELIMIT_DB", "7")

    settings = RateLimitSettings.from_environment()

    assert settings.storage_uri == (
        "rediss://user:secret@redis.example:6380/7?socket_timeout=1"
    )
