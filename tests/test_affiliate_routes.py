"""Route tests for affiliate administration and public redirects."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Any, cast

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.affiliate_repositories import AffiliateRepository
from app.db.async_session import get_async_session
from app.main import app
from app.services.affiliate_service import AffiliateService


async def _override_session() -> AsyncIterator[AsyncSession]:
    yield cast(AsyncSession, object())


def _run(coroutine_factory: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(coroutine_factory())


def _admin_headers() -> dict[str, str]:
    return {"X-Admin-API-Key": os.environ["ADMIN_API_KEY"]}


def test_admin_affiliate_routes_require_api_key() -> None:
    async def run() -> None:
        app.dependency_overrides[get_async_session] = _override_session
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                response = await client.post(
                    "/admin/api/affiliate/lookup",
                    json={"provider_id": 1, "source_job_ids": []},
                )
                assert response.status_code == 401
                assert response.json() == {"detail": "Invalid or missing admin API key"}
        finally:
            app.dependency_overrides.pop(get_async_session, None)

    _run(run)


def test_lookup_translates_service_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_lookup(
        self: AffiliateService,
        session: AsyncSession,
        provider_id: int,
        source_job_ids: list[str],
    ) -> dict[str, list[Any]]:
        assert provider_id == 7
        assert source_job_ids == ["found", "missing"]
        return {
            "matched": [
                {
                    "id": 42,
                    "source_job_id": "found",
                    "title": "Engineer",
                    "advertiser_name": None,
                    "apply_url": None,
                    "is_active": True,
                    "has_affiliate_link": True,
                    "short_hash": "existing",
                }
            ],
            "not_found": ["missing"],
        }

    monkeypatch.setattr(AffiliateService, "lookup_jobs", fake_lookup)

    async def run() -> None:
        app.dependency_overrides[get_async_session] = _override_session
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                response = await client.post(
                    "/admin/api/affiliate/lookup",
                    headers=_admin_headers(),
                    json={
                        "provider_id": 7,
                        "source_job_ids": ["found", "missing"],
                    },
                )
                assert response.status_code == 200
                assert response.json() == {
                    "matched": [
                        {
                            "job_id": 42,
                            "source_job_id": "found",
                            "title": "Engineer",
                            "advertiser_name": None,
                            "internal_job_id": 42,
                            "apply_url_available": False,
                            "has_affiliate_link": True,
                            "existing_short_hash": "existing",
                        }
                    ],
                    "not_found": ["missing"],
                }
        finally:
            app.dependency_overrides.pop(get_async_session, None)

    _run(run)


def test_generate_revalidates_and_excludes_invalid_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_lookup_by_ids(
        self: AffiliateRepository,
        provider_id: int,
        job_ids: list[int],
    ) -> list[dict[str, Any]]:
        assert provider_id == 7
        assert job_ids == [1, 2, 3]
        return [
            {"id": 1, "apply_url": "https://example.test/apply"},
            {"id": 2, "apply_url": None},
        ]

    async def fake_generate(
        self: AffiliateService,
        session: AsyncSession,
        provider_id: int,
        job_ids: list[int],
        admin_id: Any = None,
    ) -> list[dict[str, Any]]:
        assert provider_id == 7
        assert job_ids == [1]
        assert admin_id is None
        return [{"job_id": 1, "short_hash": "newhash", "redirect_url": "/r/newhash"}]

    monkeypatch.setattr(AffiliateRepository, "lookup_jobs_by_ids", fake_lookup_by_ids)
    monkeypatch.setattr(AffiliateService, "generate_links", fake_generate)

    async def run() -> None:
        app.dependency_overrides[get_async_session] = _override_session
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                response = await client.post(
                    "/admin/api/affiliate/generate",
                    headers=_admin_headers(),
                    json={"provider_id": 7, "job_ids": [1, 2, 3]},
                )
                assert response.status_code == 200
                assert response.json() == {
                    "generated": [
                        {
                            "job_id": 1,
                            "short_hash": "newhash",
                            "redirect_url": "/r/newhash",
                        }
                    ],
                    "excluded": [
                        {"job_id": 2, "reason": "Apply URL is unavailable"},
                        {"job_id": 3, "reason": "Job not found for provider"},
                    ],
                }
        finally:
            app.dependency_overrides.pop(get_async_session, None)

    _run(run)


def test_redirect_resolves_inactive_jobs_and_renders_missing_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_get(
        self: AffiliateRepository, short_hash: str
    ) -> dict[str, Any] | None:
        if short_hash == "known":
            return {
                "short_hash": "known",
                "apply_url": "https://example.test/apply",
                "is_active": False,
                "slug": "expired-job",
            }
        return None

    monkeypatch.setattr(AffiliateRepository, "get_by_short_hash", fake_get)

    async def run() -> None:
        app.dependency_overrides[get_async_session] = _override_session
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
                follow_redirects=False,
            ) as client:
                found = await client.get("/r/known")
                assert found.status_code == 302
                assert found.headers["location"] == "https://example.test/apply"

                missing = await client.get("/r/missing")
                assert missing.status_code == 404
                assert "This job is no longer available" in missing.text
                assert 'href="/"' in missing.text
        finally:
            app.dependency_overrides.pop(get_async_session, None)

    _run(run)
