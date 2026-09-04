"""Integration tests for GET /api/v1/jobs/{slug}."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Coroutine, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
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
EXPECTED_KEYS = {
    "id",
    "slug",
    "title",
    "classification",
    "employment_type",
    "country_name",
    "location",
    "apply_url",
    "last_imported_at",
    "remote_status",
    "remote_status_source",
    "experience_level",
    "experience_level_source",
    "description",
    "advertiser_name",
    "salary_min",
    "salary_max",
    "salary_currency",
    "salary_period",
    "created_at",
    "source_updated_at",
    "is_expired",
    "structured_data",
}


def _async_url(sync_url: str) -> str:
    return sync_url.replace("postgresql://", "postgresql+asyncpg://", 1)


def _job_values(*, index: int, is_active: bool = True) -> dict[str, Any]:
    return {
        "source_name": "jobg8",
        "source_job_id": f"detail-test-{index}",
        "slug": f"detail-test-job-{index}",
        "title": "Principal Decimal Engineer",
        "description": "Build precise financial systems.",
        "advertiser_name": "Exact Systems",
        "classification": "Information Technology",
        "employment_type": "Full Time",
        "country_name": "Australia",
        "location": "Sydney",
        "area": "New South Wales",
        "postal_code": "2000",
        "apply_url": f"https://example.test/apply/detail/{index}",
        "salary_min": Decimal("123456789012.34"),
        "salary_max": Decimal("123456789012.35"),
        "salary_currency": "AUD",
        "salary_period": "Annual",
        "source_payload": {"SenderReference": f"detail-test-{index}"},
        "payload_hash": f"detail-hash-{index}",
        "is_active": is_active,
        "deactivated_at": None if is_active else BASE_TIME,
        "first_imported_at": BASE_TIME - timedelta(days=1),
        "last_imported_at": BASE_TIME,
        "created_at": BASE_TIME - timedelta(days=2),
        "content_updated_at": BASE_TIME - timedelta(hours=1),
        "remote_status": "hybrid",
        "remote_status_source": "inferred",
        "experience_level": "senior",
        "experience_level_source": "inferred",
    }


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


def test_active_job_returns_full_public_detail(test_database_url: str) -> None:
    row = _job_values(index=1)

    async def run() -> None:
        async with _api(test_database_url, [row]) as client:
            response = await client.get("/api/v1/jobs/detail-test-job-1")

            assert response.status_code == 200
            body = response.json()
            assert set(body) == EXPECTED_KEYS
            assert body["is_expired"] is False
            assert body["description"] == row["description"]
            assert body["advertiser_name"] == row["advertiser_name"]
            assert body["apply_url"] == row["apply_url"]
            assert body["remote_status"] == row["remote_status"]
            assert body["remote_status_source"] == row["remote_status_source"]
            assert body["experience_level"] == row["experience_level"]
            assert body["experience_level_source"] == row["experience_level_source"]
            assert (
                datetime.fromisoformat(body["source_updated_at"])
                == row["content_updated_at"]
            )
            structured_data = body["structured_data"]
            assert structured_data["@context"] == "https://schema.org/"
            assert structured_data["@type"] == "JobPosting"
            assert structured_data["title"] == row["title"]
            assert structured_data["description"] == row["description"]
            assert structured_data["identifier"] == {
                "@type": "PropertyValue",
                "name": "job-board",
                "value": str(body["id"]),
            }
            assert structured_data["datePosted"] == "2026-07-31T12:00:00+00:00"
            assert structured_data["employmentType"] == "FULL_TIME"
            assert structured_data["hiringOrganization"] == {
                "@type": "Organization",
                "name": "Exact Systems",
            }
            assert structured_data["jobLocation"] == {
                "@type": "Place",
                "address": {
                    "@type": "PostalAddress",
                    "addressLocality": "Sydney",
                    "addressRegion": "New South Wales",
                    "postalCode": "2000",
                    "addressCountry": "Australia",
                },
            }
            assert structured_data["baseSalary"] == {
                "@type": "MonetaryAmount",
                "currency": "AUD",
                "value": {
                    "@type": "QuantitativeValue",
                    "minValue": "123456789012.34",
                    "maxValue": "123456789012.35",
                    "unitText": "YEAR",
                },
            }
            assert structured_data["directApply"] is True
            assert structured_data["url"] == row["apply_url"]
            assert "validThrough" not in structured_data

    _run(run)


def test_soft_deleted_job_returns_full_expired_detail(
    test_database_url: str,
) -> None:
    row = _job_values(index=2, is_active=False)

    async def run() -> None:
        async with _api(test_database_url, [row]) as client:
            response = await client.get("/api/v1/jobs/detail-test-job-2")

            assert response.status_code == 200
            body = response.json()
            assert set(body) == EXPECTED_KEYS
            assert body["is_expired"] is True
            assert body["title"] == row["title"]
            assert body["description"] == row["description"]
            assert body["apply_url"] == row["apply_url"]
            assert body["structured_data"] is None

    _run(run)


def test_unknown_slug_returns_404(test_database_url: str) -> None:
    async def run() -> None:
        async with _api(test_database_url, []) as client:
            response = await client.get("/api/v1/jobs/does-not-exist")

            assert response.status_code == 404
            assert response.json() == {
                "error": {
                    "code": "NOT_FOUND",
                    "message": "Job not found",
                    "details": None,
                }
            }

    _run(run)


def test_detail_response_never_leaks_internal_columns(
    test_database_url: str,
) -> None:
    async def run() -> None:
        async with _api(test_database_url, [_job_values(index=3)]) as client:
            response = await client.get("/api/v1/jobs/detail-test-job-3")

            assert response.status_code == 200
            assert set(response.json()) == EXPECTED_KEYS

    _run(run)


def test_active_job_has_non_empty_apply_url(test_database_url: str) -> None:
    async def run() -> None:
        async with _api(test_database_url, [_job_values(index=4)]) as client:
            response = await client.get("/api/v1/jobs/detail-test-job-4")

            assert response.status_code == 200
            assert response.json()["apply_url"]

    _run(run)


def test_salary_decimals_round_trip_without_precision_drift(
    test_database_url: str,
) -> None:
    async def run() -> None:
        async with _api(test_database_url, [_job_values(index=5)]) as client:
            response = await client.get("/api/v1/jobs/detail-test-job-5")

            assert response.status_code == 200
            body = response.json()
            # Pydantic v2 serializes Decimal to a JSON string, retaining the
            # exact value instead of passing through binary floating point.
            assert body["salary_min"] == "123456789012.34"
            assert body["salary_max"] == "123456789012.35"

    _run(run)


def test_structured_data_omits_missing_salary(test_database_url: str) -> None:
    row = _job_values(index=6)
    row.update(salary_min=None, salary_max=None)

    async def run() -> None:
        async with _api(test_database_url, [row]) as client:
            response = await client.get("/api/v1/jobs/detail-test-job-6")

            assert response.status_code == 200
            assert "baseSalary" not in response.json()["structured_data"]

    _run(run)


def test_structured_data_omits_missing_location_subfields(
    test_database_url: str,
) -> None:
    row = _job_values(index=7)
    row.update(location=None, area="", postal_code=None)

    async def run() -> None:
        async with _api(test_database_url, [row]) as client:
            response = await client.get("/api/v1/jobs/detail-test-job-7")

            assert response.status_code == 200
            address = response.json()["structured_data"]["jobLocation"]["address"]
            assert address == {
                "@type": "PostalAddress",
                "addressCountry": "Australia",
            }

    _run(run)
