"""Integration tests for GET /api/v1/jobs/filters."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Coroutine, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.db.async_session import get_async_session
from app.db.models import Job, Provider
from app.main import app

BASE_TIME = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)


def _async_url(sync_url: str) -> str:
    return sync_url.replace("postgresql://", "postgresql+asyncpg://", 1)


def _job_values(
    *,
    index: int,
    classification: str | None,
    employment_type: str | None,
    country_name: str | None,
    is_active: bool = True,
) -> dict[str, Any]:
    return {
        "source_name": "jobg8",
        "source_job_id": f"filter-test-{index}",
        "slug": f"filter-test-job-{index}",
        "title": f"Filter Test Job {index}",
        "description": "A job used to verify filter metadata.",
        "classification": classification,
        "employment_type": employment_type,
        "country_name": country_name,
        "location": "Sydney",
        "apply_url": f"https://example.test/apply/filter/{index}",
        "source_payload": {"SenderReference": f"filter-test-{index}"},
        "payload_hash": f"filter-hash-{index}",
        "is_active": is_active,
        "deactivated_at": None if is_active else BASE_TIME,
        "first_imported_at": BASE_TIME - timedelta(days=1),
        "last_imported_at": BASE_TIME,
    }


def _varied_rows() -> list[dict[str, Any]]:
    return [
        _job_values(
            index=1,
            classification="Engineering",
            employment_type="Full Time",
            country_name="Australia",
        ),
        _job_values(
            index=2,
            classification="Engineering",
            employment_type="Contract",
            country_name="Australia",
        ),
        _job_values(
            index=3,
            classification="Healthcare",
            employment_type="Full Time",
            country_name="New Zealand",
        ),
        _job_values(
            index=4,
            classification=None,
            employment_type=None,
            country_name=None,
        ),
        _job_values(
            index=5,
            classification="Engineering",
            employment_type="Full Time",
            country_name="Australia",
            is_active=False,
        ),
    ]


@asynccontextmanager
async def _api(
    database_url: str, rows: Sequence[dict[str, Any]]
) -> AsyncIterator[httpx.AsyncClient]:
    engine = create_async_engine(_async_url(database_url), poolclass=NullPool)
    session_factory = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False
    )

    try:
        async with session_factory() as setup_session:
            await setup_session.execute(delete(Job))
            provider_id = (
                await setup_session.execute(
                    select(Provider.id).where(Provider.name == "jobg8-test")
                )
            ).scalar_one_or_none()
            if provider_id is None:
                provider_id = (
                    await setup_session.execute(
                        insert(Provider)
                        .values(name="jobg8-test", format="xml")
                        .returning(Provider.id)
                    )
                ).scalar_one()
            if rows:
                await setup_session.execute(
                    insert(Job),
                    [dict(row, provider_id=provider_id) for row in rows],
                )
            await setup_session.commit()

        async def override_session() -> AsyncIterator[AsyncSession]:
            async with session_factory() as session:
                yield session

        app.dependency_overrides[get_async_session] = override_session
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_async_session, None)
        await engine.dispose()


def _run(coroutine_factory: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(coroutine_factory())


def test_only_active_jobs_contribute_to_filter_metadata(
    test_database_url: str,
) -> None:
    async def run() -> None:
        async with _api(test_database_url, _varied_rows()) as client:
            response = await client.get("/api/v1/jobs/filters")

            assert response.status_code == 200
            body = response.json()
            assert body["classifications"] == [
                {"value": "Engineering", "count": 2},
                {"value": "Healthcare", "count": 1},
            ]
            assert body["employment_types"] == [
                {"value": "Full Time", "count": 2},
                {"value": "Contract", "count": 1},
            ]
            assert body["country_names"] == [
                {"value": "Australia", "count": 2},
                {"value": "New Zealand", "count": 1},
            ]

    _run(run)


def test_filter_counts_are_exact(test_database_url: str) -> None:
    async def run() -> None:
        async with _api(test_database_url, _varied_rows()) as client:
            response = await client.get("/api/v1/jobs/filters")

            body = response.json()
            assert {
                item["value"]: item["count"] for item in body["classifications"]
            } == {
                "Engineering": 2,
                "Healthcare": 1,
            }
            assert {
                item["value"]: item["count"] for item in body["employment_types"]
            } == {"Full Time": 2, "Contract": 1}
            assert {item["value"]: item["count"] for item in body["country_names"]} == {
                "Australia": 2,
                "New Zealand": 1,
            }

    _run(run)


def test_null_filter_values_are_excluded(test_database_url: str) -> None:
    async def run() -> None:
        async with _api(test_database_url, _varied_rows()) as client:
            response = await client.get("/api/v1/jobs/filters")

            body = response.json()
            for options in body.values():
                assert all(option["value"] is not None for option in options)

    _run(run)


def test_zero_active_jobs_returns_empty_filter_lists(
    test_database_url: str,
) -> None:
    inactive = _job_values(
        index=6,
        classification="Engineering",
        employment_type="Full Time",
        country_name="Australia",
        is_active=False,
    )

    async def run() -> None:
        async with _api(test_database_url, [inactive]) as client:
            response = await client.get("/api/v1/jobs/filters")

            assert response.status_code == 200
            assert response.json() == {
                "classifications": [],
                "employment_types": [],
                "country_names": [],
            }

    _run(run)


def test_filters_route_is_not_swallowed_by_slug_route(
    test_database_url: str,
) -> None:
    async def run() -> None:
        async with _api(test_database_url, []) as client:
            response = await client.get("/api/v1/jobs/filters")

            assert response.status_code == 200
            assert response.json() == {
                "classifications": [],
                "employment_types": [],
                "country_names": [],
            }
            assert response.json() != {"detail": "Job not found"}

    _run(run)
