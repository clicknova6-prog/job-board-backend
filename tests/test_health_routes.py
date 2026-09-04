"""Tests for GET /health (liveness) and GET /health/ready (readiness)."""

from __future__ import annotations

from typing import Any
from unittest.mock import Mock

import pytest
import redis.asyncio as async_redis
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisConnectionError

from app.core.rate_limit import limiter
from app.db.async_session import get_async_session
from app.main import app


class _FakeSession:
    def __init__(self, *, execute_error: Exception | None = None) -> None:
        self._execute_error = execute_error

    async def execute(self, *args: Any, **kwargs: Any) -> None:
        if self._execute_error is not None:
            raise self._execute_error


class _FakeRedisClient:
    def __init__(self, *, ping_error: Exception | None = None) -> None:
        self._ping_error = ping_error
        self.closed = False

    async def ping(self) -> bool:
        if self._ping_error is not None:
            raise self._ping_error
        return True

    async def aclose(self) -> None:
        self.closed = True


def _override_session(session: _FakeSession):
    async def _get():
        yield session

    return _get


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.pop(get_async_session, None)


def test_liveness_returns_ok_even_when_db_and_redis_are_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app.dependency_overrides[get_async_session] = _override_session(
        _FakeSession(execute_error=RuntimeError("database down"))
    )
    monkeypatch.setattr(
        async_redis.Redis,
        "from_url",
        staticmethod(
            lambda *a, **kw: _FakeRedisClient(
                ping_error=RedisConnectionError("redis down")
            )
        ),
    )

    response = TestClient(app).get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_returns_200_when_db_and_redis_are_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app.dependency_overrides[get_async_session] = _override_session(_FakeSession())
    monkeypatch.setattr(
        async_redis.Redis,
        "from_url",
        staticmethod(lambda *a, **kw: _FakeRedisClient()),
    )

    response = TestClient(app).get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_returns_503_when_database_check_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app.dependency_overrides[get_async_session] = _override_session(
        _FakeSession(execute_error=RuntimeError("database down"))
    )
    monkeypatch.setattr(
        async_redis.Redis,
        "from_url",
        staticmethod(lambda *a, **kw: _FakeRedisClient()),
    )

    response = TestClient(app).get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {
        "error": {
            "code": "SERVICE_UNAVAILABLE",
            "message": "Readiness check failed: database",
            "details": None,
        }
    }


def test_readiness_returns_503_when_redis_check_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app.dependency_overrides[get_async_session] = _override_session(_FakeSession())
    monkeypatch.setattr(
        async_redis.Redis,
        "from_url",
        staticmethod(
            lambda *a, **kw: _FakeRedisClient(
                ping_error=RedisConnectionError("redis down")
            )
        ),
    )

    response = TestClient(app).get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {
        "error": {
            "code": "SERVICE_UNAVAILABLE",
            "message": "Readiness check failed: redis",
            "details": None,
        }
    }


def test_health_routes_never_touch_rate_limit_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app.dependency_overrides[get_async_session] = _override_session(_FakeSession())
    monkeypatch.setattr(
        async_redis.Redis,
        "from_url",
        staticmethod(lambda *a, **kw: _FakeRedisClient()),
    )
    redis_increment = Mock(side_effect=AssertionError("rate limit counter touched"))
    monkeypatch.setattr(limiter._storage, "incr", redis_increment)

    client = TestClient(app)
    responses = [client.get("/health"), client.get("/health/ready")]

    assert [response.status_code for response in responses] == [200, 200]
    redis_increment.assert_not_called()
