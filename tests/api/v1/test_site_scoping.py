"""Integration tests: public job endpoints are scoped to the X-Site-Key site."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import create_engine, delete, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.core.site_cors import site_origin_registry
from app.db.async_session import get_async_session
from app.db.models import Job, JobSite, Provider, Site
from app.db.repositories import SitemapRepository
from app.db.site_repositories import PublicSiteRepository
from app.main import app

BASE_TIME = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
IT_KEY = "it-key"
MED_KEY = "med-key"
OFF_KEY = "off-key"


def _job(index: int, classification: str) -> dict[str, Any]:
    return {
        "source_name": "jobg8",
        "source_job_id": f"scope-{index}",
        "slug": f"scope-job-{index}",
        "title": f"Job {index}",
        "description": "Description.",
        "classification": classification,
        "apply_url": f"https://example.test/apply/{index}",
        "source_payload": {"SenderReference": f"scope-{index}"},
        "payload_hash": f"scope-hash-{index}",
        "is_active": True,
        "deactivated_at": None,
        "first_imported_at": BASE_TIME,
        "last_imported_at": BASE_TIME + timedelta(minutes=index),
    }


@asynccontextmanager
async def _api(database_url: str) -> AsyncIterator[httpx.AsyncClient]:
    """Seed sites IT (jobs 0-1, shared 4), Medical (2-3, shared 4), inactive Off."""
    engine = create_async_engine(
        database_url.replace("postgresql://", "postgresql+asyncpg://", 1),
        poolclass=NullPool,
    )
    factory = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False
    )
    original_loader = site_origin_registry._loader

    try:
        async with factory() as s:
            await s.execute(delete(Job))
            await s.execute(delete(Site))
            provider_id = (
                await s.execute(
                    select(Provider.id).where(Provider.name == "jobg8-test")
                )
            ).scalar_one_or_none() or (
                await s.execute(
                    insert(Provider)
                    .values(name="jobg8-test", format="xml")
                    .returning(Provider.id)
                )
            ).scalar_one()
            classes = ["IT", "IT", "Medical", "Medical", "Shared"]
            job_ids = list(
                (
                    await s.execute(
                        insert(Job).returning(Job.id),
                        [
                            dict(_job(i, c), provider_id=provider_id)
                            for i, c in enumerate(classes)
                        ],
                    )
                ).scalars()
            )
            sites = {}
            for slug, key, base, active, cfg in [
                (
                    "it",
                    IT_KEY,
                    "https://it.example.test",
                    True,
                    {"cors_origins": ["https://www.it.example.test"]},
                ),
                ("med", MED_KEY, "https://med.example.test", True, {}),
                ("off", OFF_KEY, "https://off.example.test", False, {}),
            ]:
                sites[slug] = (
                    await s.execute(
                        insert(Site)
                        .values(
                            slug=slug,
                            name=slug,
                            site_key=key,
                            base_url=base,
                            is_active=active,
                            config=cfg,
                        )
                        .returning(Site.id)
                    )
                ).scalar_one()
            assignments = [
                (0, "it"),
                (1, "it"),
                (4, "it"),
                (2, "med"),
                (3, "med"),
                (4, "med"),
                (0, "off"),
            ]
            await s.execute(
                insert(JobSite),
                [
                    {"job_id": job_ids[i], "site_id": sites[slug]}
                    for i, slug in assignments
                ],
            )
            await s.commit()

        async def override_session() -> AsyncIterator[AsyncSession]:
            async with factory() as session:
                yield session

        async def load_origins() -> frozenset[str]:
            async with factory() as session:
                return await PublicSiteRepository(session).list_active_allowed_origins()

        site_origin_registry._loader = load_origins
        site_origin_registry.invalidate()
        app.dependency_overrides[get_async_session] = override_session
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_async_session, None)
        site_origin_registry._loader = original_loader
        site_origin_registry.invalidate()
        await engine.dispose()


def _slugs(response: httpx.Response) -> set[str]:
    assert response.status_code == 200, response.text
    return {item["slug"] for item in response.json()["items"]}


@pytest.mark.parametrize(
    "path", ["/api/v1/jobs", "/api/v1/jobs/filters", "/api/v1/jobs/scope-job-0"]
)
def test_missing_unknown_and_inactive_site_keys_are_rejected(
    test_database_url: str, path: str
) -> None:
    async def run() -> None:
        async with _api(test_database_url) as client:
            missing = await client.get(path)
            assert missing.status_code == 400
            assert missing.json()["error"]["code"] == "BAD_REQUEST"
            assert (
                await client.get(path, headers={"X-Site-Key": "nope"})
            ).status_code == 404
            assert (
                await client.get(path, headers={"X-Site-Key": OFF_KEY})
            ).status_code == 404

    asyncio.run(run())


def test_listing_returns_only_the_sites_jobs(test_database_url: str) -> None:
    async def run() -> None:
        async with _api(test_database_url) as client:
            it = await client.get("/api/v1/jobs", headers={"X-Site-Key": IT_KEY})
            med = await client.get("/api/v1/jobs", headers={"X-Site-Key": MED_KEY})
            assert _slugs(it) == {"scope-job-0", "scope-job-1", "scope-job-4"}
            assert _slugs(med) == {"scope-job-2", "scope-job-3", "scope-job-4"}

    asyncio.run(run())


def test_detail_of_another_sites_job_is_not_found(test_database_url: str) -> None:
    async def run() -> None:
        async with _api(test_database_url) as client:
            headers = {"X-Site-Key": IT_KEY}
            assert (
                await client.get("/api/v1/jobs/scope-job-0", headers=headers)
            ).status_code == 200
            assert (
                await client.get("/api/v1/jobs/scope-job-4", headers=headers)
            ).status_code == 200
            assert (
                await client.get("/api/v1/jobs/scope-job-2", headers=headers)
            ).status_code == 404

    asyncio.run(run())


def test_filters_are_computed_and_cached_per_site(test_database_url: str) -> None:
    async def run() -> None:
        async with _api(test_database_url) as client:
            for _ in range(2):  # second round is served from the cache
                it = await client.get(
                    "/api/v1/jobs/filters", headers={"X-Site-Key": IT_KEY}
                )
                med = await client.get(
                    "/api/v1/jobs/filters", headers={"X-Site-Key": MED_KEY}
                )
                assert {
                    o["value"]: o["count"] for o in it.json()["classifications"]
                } == {"IT": 2, "Shared": 1}
                assert {
                    o["value"]: o["count"] for o in med.json()["classifications"]
                } == {"Medical": 2, "Shared": 1}

    asyncio.run(run())


@pytest.mark.parametrize(
    ("origin", "expected"),
    [
        ("https://it.example.test", 200),  # site base_url
        ("https://www.it.example.test", 200),  # site config.cors_origins
        ("http://localhost:3000", 200),  # hub-wide env origin
        ("https://med.example.test", 403),  # another site's origin
        ("https://evil.example", 403),
        ("null", 403),
    ],
)
def test_browser_origin_must_belong_to_the_site(
    test_database_url: str, origin: str, expected: int
) -> None:
    async def run() -> None:
        async with _api(test_database_url) as client:
            response = await client.get(
                "/api/v1/jobs", headers={"X-Site-Key": IT_KEY, "Origin": origin}
            )
            assert response.status_code == expected

    asyncio.run(run())


@pytest.mark.parametrize(
    ("origin", "allowed"),
    [
        ("https://it.example.test", True),
        ("https://www.it.example.test", True),
        ("https://med.example.test", True),
        ("https://off.example.test", False),  # deactivated site
        ("https://evil.example", False),
    ],
)
def test_cors_preflight_allows_active_site_origins(
    test_database_url: str, origin: str, allowed: bool
) -> None:
    async def run() -> None:
        async with _api(test_database_url) as client:
            response = await client.options(
                "/api/v1/jobs",
                headers={
                    "Origin": origin,
                    "Access-Control-Request-Method": "GET",
                    "Access-Control-Request-Headers": "x-site-key",
                },
            )
            if allowed:
                assert response.status_code == 200
                assert response.headers["access-control-allow-origin"] == origin
            else:
                assert response.status_code == 400
                assert "access-control-allow-origin" not in response.headers

    asyncio.run(run())


def _set_priority(database_url: str, slug: str, priority: int) -> None:
    engine = create_engine(
        database_url.replace("postgresql://", "postgresql+psycopg://", 1),
        poolclass=NullPool,
    )
    with Session(engine) as session:
        session.execute(
            update(Site).where(Site.slug == slug).values(canonical_priority=priority)
        )
        session.commit()
    engine.dispose()


def _sitemap_slugs(database_url: str) -> dict[str, set[str]]:
    engine = create_engine(
        database_url.replace("postgresql://", "postgresql+psycopg://", 1),
        poolclass=NullPool,
    )
    with Session(engine) as session:
        repo = SitemapRepository(session)
        result = {
            site.slug: {
                job.slug
                for job in repo.list_active_jobs_after(
                    site_id=site.id,
                    canonical_priority=site.canonical_priority,
                    after_id=0,
                    limit=100,
                )
            }
            for site in repo.list_active_sites()
        }
    engine.dispose()
    return result


def test_shared_job_has_one_canonical_owner(test_database_url: str) -> None:
    """Job 4 is on IT and Medical; equal priority -> lower site id (IT) owns."""

    async def run(owner_key: str, other_key: str, owner_base: str) -> None:
        async with _api(test_database_url) as client:
            if owner_key == MED_KEY:
                _set_priority(test_database_url, "med", 10)
            owner = (
                await client.get(
                    "/api/v1/jobs/scope-job-4", headers={"X-Site-Key": owner_key}
                )
            ).json()
            other = (
                await client.get(
                    "/api/v1/jobs/scope-job-4", headers={"X-Site-Key": other_key}
                )
            ).json()
            expected = f"{owner_base}/job/scope-job-4"
            assert owner["canonical_url"] == other["canonical_url"] == expected
            assert owner["structured_data"]["url"] == expected
            assert other["structured_data"] is None
            sitemaps = _sitemap_slugs(test_database_url)
            owner_slug = "med" if owner_key == MED_KEY else "it"
            other_slug = "it" if owner_key == MED_KEY else "med"
            assert "scope-job-4" in sitemaps[owner_slug]
            assert "scope-job-4" not in sitemaps[other_slug]

    asyncio.run(run(IT_KEY, MED_KEY, "https://it.example.test"))
    asyncio.run(run(MED_KEY, IT_KEY, "https://med.example.test"))
