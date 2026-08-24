"""Integration tests for GET /api/v1/jobs against a real PostgreSQL database."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator, Callable, Coroutine, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.api.v1.cursors import decode_cursor, encode_cursor
from app.db.async_session import get_async_session
from app.db.models import Job, Provider
from app.main import app

BASE_TIME = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)


def _async_url(sync_url: str) -> str:
    """Convert the conftest psycopg URL to the asyncpg dialect."""
    return sync_url.replace("postgresql://", "postgresql+asyncpg://", 1)


def _job_values(
    *,
    index: int,
    last_imported_at: datetime,
    is_active: bool = True,
    title: str = "Software Engineer",
    description: str = "Build and maintain backend services.",
    classification: str | None = "Information Technology",
    employment_type: str | None = "Full Time",
    country_name: str | None = "Australia",
    location: str | None = "Sydney",
) -> dict[str, Any]:
    """Build one INSERT row that satisfies every jobs check constraint."""
    return {
        "source_name": "jobg8",
        "source_job_id": f"test-{index}",
        "slug": f"test-job-{index}",
        "title": title,
        "description": description,
        "classification": classification,
        "employment_type": employment_type,
        "country_name": country_name,
        "location": location,
        "apply_url": f"https://example.test/apply/{index}",
        "source_payload": {"SenderReference": f"test-{index}"},
        "payload_hash": f"hash-{index}",
        "is_active": is_active,
        # The active/deactivated consistency constraint ties these together.
        "deactivated_at": None if is_active else last_imported_at,
        "first_imported_at": BASE_TIME - timedelta(days=1),
        "last_imported_at": last_imported_at,
        "remote_status": "remote",
        "remote_status_source": "inferred",
        "experience_level": "mid",
        "experience_level_source": "inferred",
    }


@asynccontextmanager
async def _api(
    database_url: str, rows: Sequence[dict[str, Any]]
) -> AsyncIterator[httpx.AsyncClient]:
    """Yield an HTTP client bound to a freshly seeded test database.

    The engine is built inside the running loop because each test drives its
    own ``asyncio.run``, and an asyncpg pool cannot cross event loops.
    """
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
    """Run one async test body, matching the project's existing test style."""
    asyncio.run(coroutine_factory())


def _encode_cursor_payload(payload: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()


def test_inactive_jobs_are_never_returned(test_database_url: str) -> None:
    rows = [
        _job_values(
            index=index,
            last_imported_at=BASE_TIME + timedelta(minutes=index),
            is_active=index % 2 == 0,
        )
        for index in range(6)
    ]

    async def run() -> None:
        async with _api(test_database_url, rows) as client:
            response = await client.get("/api/v1/jobs", params={"limit": 100})
            assert response.status_code == 200
            slugs = {item["slug"] for item in response.json()["items"]}
            assert slugs == {"test-job-0", "test-job-2", "test-job-4"}

    _run(run)


def test_keyset_pagination_covers_every_row_without_duplicates(
    test_database_url: str,
) -> None:
    total = 25
    rows = [
        _job_values(index=index, last_imported_at=BASE_TIME + timedelta(minutes=index))
        for index in range(total)
    ]

    async def run() -> None:
        async with _api(test_database_url, rows) as client:
            seen: list[int] = []
            seen_timestamps: list[str] = []
            cursor: str | None = None

            for page_number in range(3):
                params: dict[str, Any] = {"limit": 10}
                if cursor is not None:
                    params["cursor"] = cursor
                response = await client.get("/api/v1/jobs", params=params)
                assert response.status_code == 200
                body = response.json()

                expected_size = 10 if page_number < 2 else 5
                assert len(body["items"]) == expected_size
                seen.extend(item["id"] for item in body["items"])
                seen_timestamps.extend(
                    item["last_imported_at"] for item in body["items"]
                )

                if page_number < 2:
                    assert body["has_more"] is True
                    assert body["next_cursor"] is not None
                    cursor = body["next_cursor"]
                else:
                    assert body["has_more"] is False
                    assert body["next_cursor"] is None

            assert len(seen) == total
            assert len(set(seen)) == total, "a job appeared on more than one page"
            # Default sort is newest first, and the seeded timestamps ascend
            # with the row index, so the ids must come back in reverse order.
            assert seen == sorted(seen, reverse=True)
            assert seen_timestamps == sorted(seen_timestamps, reverse=True)

    _run(run)


def test_page_is_truncated_before_crossing_result_cap(
    test_database_url: str,
) -> None:
    rows = [
        _job_values(index=index, last_imported_at=BASE_TIME + timedelta(minutes=index))
        for index in range(102)
    ]

    async def run() -> None:
        async with _api(test_database_url, rows) as client:
            first = await client.get("/api/v1/jobs", params={"limit": 1})
            assert first.status_code == 200
            issued_cursor = decode_cursor(first.json()["next_cursor"])
            near_cap_cursor = encode_cursor(replace(issued_cursor, served_count=9_950))

            response = await client.get(
                "/api/v1/jobs",
                params={"limit": 100, "cursor": near_cap_cursor},
            )

            assert response.status_code == 200
            body = response.json()
            assert len(body["items"]) == 50
            assert body["has_more"] is False
            assert body["next_cursor"] is None

    _run(run)


def test_cursor_at_9999_served_returns_at_most_one_item(
    test_database_url: str,
) -> None:
    rows = [
        _job_values(index=index, last_imported_at=BASE_TIME + timedelta(minutes=index))
        for index in range(4)
    ]

    async def run() -> None:
        async with _api(test_database_url, rows) as client:
            first = await client.get("/api/v1/jobs", params={"limit": 1})
            assert first.status_code == 200
            issued_cursor = decode_cursor(first.json()["next_cursor"])
            near_cap_cursor = encode_cursor(replace(issued_cursor, served_count=9_999))

            response = await client.get(
                "/api/v1/jobs",
                params={"limit": 100, "cursor": near_cap_cursor},
            )

            assert response.status_code == 200
            body = response.json()
            assert len(body["items"]) == 1
            assert body["has_more"] is False
            assert body["next_cursor"] is None

    _run(run)


def test_ascending_sort_reverses_the_page_order(test_database_url: str) -> None:
    rows = [
        _job_values(index=index, last_imported_at=BASE_TIME + timedelta(minutes=index))
        for index in range(5)
    ]

    async def run() -> None:
        async with _api(test_database_url, rows) as client:
            collected: list[str] = []
            cursor: str | None = None
            while True:
                params: dict[str, Any] = {"limit": 2, "sort": "-last_imported_at"}
                if cursor is not None:
                    params["cursor"] = cursor
                response = await client.get("/api/v1/jobs", params=params)
                assert response.status_code == 200
                body = response.json()
                collected.extend(item["slug"] for item in body["items"])
                if not body["has_more"]:
                    assert body["next_cursor"] is None
                    break
                cursor = body["next_cursor"]

            assert collected == [f"test-job-{index}" for index in range(5)]

    _run(run)


@pytest.mark.parametrize(
    ("sort", "expect_descending_ids"),
    [("last_imported_at", True), ("-last_imported_at", False)],
)
def test_identical_timestamps_split_across_pages_stay_stable(
    test_database_url: str, sort: str, expect_descending_ids: bool
) -> None:
    """Rows sharing last_imported_at must tiebreak on id across a page split.

    The tiebreak direction follows the primary sort direction, so the pair
    comes back id DESC under the default sort and id ASC under
    "-last_imported_at". What must hold in both cases is the part that
    matters: each row appears exactly once, none is skipped, and the paged
    order matches the unpaged order.
    """
    shared = BASE_TIME + timedelta(hours=1)
    rows = [
        _job_values(index=0, last_imported_at=shared),
        _job_values(index=1, last_imported_at=shared),
    ]

    async def run() -> None:
        async with _api(test_database_url, rows) as client:
            first = await client.get("/api/v1/jobs", params={"limit": 1, "sort": sort})
            assert first.status_code == 200
            first_body = first.json()
            assert first_body["has_more"] is True
            assert len(first_body["items"]) == 1

            second = await client.get(
                "/api/v1/jobs",
                params={"limit": 1, "sort": sort, "cursor": first_body["next_cursor"]},
            )
            assert second.status_code == 200
            second_body = second.json()
            assert len(second_body["items"]) == 1
            assert second_body["has_more"] is False
            assert second_body["next_cursor"] is None

            first_id = first_body["items"][0]["id"]
            second_id = second_body["items"][0]["id"]

            # Neither duplicated nor skipped: two distinct rows, and they are
            # exactly the two that were seeded.
            assert first_id != second_id, "the tied row was repeated across pages"
            unpaged = await client.get(
                "/api/v1/jobs", params={"limit": 10, "sort": sort}
            )
            unpaged_ids = [item["id"] for item in unpaged.json()["items"]]
            assert len(unpaged_ids) == 2
            assert set(unpaged_ids) == {first_id, second_id}

            # Paging must not reorder relative to an unpaged read.
            assert unpaged_ids == [first_id, second_id]

            if expect_descending_ids:
                assert first_id > second_id
            else:
                assert first_id < second_id

    _run(run)


@pytest.mark.parametrize(
    ("parameter", "value"),
    [
        ("classification", "Healthcare"),
        ("employment_type", "Contract"),
        ("country_name", "New Zealand"),
        ("location", "Wellington"),
        ("q", "phlebotomy"),
    ],
)
def test_each_filter_narrows_the_result_set(
    test_database_url: str, parameter: str, value: str
) -> None:
    rows = [
        _job_values(index=0, last_imported_at=BASE_TIME),
        _job_values(
            index=1,
            last_imported_at=BASE_TIME + timedelta(minutes=1),
            title="Registered Nurse",
            description="Ward rotation including phlebotomy duties.",
            classification="Healthcare",
            employment_type="Contract",
            country_name="New Zealand",
            location="Wellington",
        ),
    ]

    async def run() -> None:
        async with _api(test_database_url, rows) as client:
            unfiltered = await client.get("/api/v1/jobs", params={"limit": 100})
            assert len(unfiltered.json()["items"]) == 2

            response = await client.get(
                "/api/v1/jobs", params={"limit": 100, parameter: value}
            )
            assert response.status_code == 200
            items = response.json()["items"]
            assert [item["slug"] for item in items] == ["test-job-1"]

    _run(run)


def test_full_text_search_matches_stemmed_terms(test_database_url: str) -> None:
    rows = [
        _job_values(
            index=0,
            last_imported_at=BASE_TIME,
            title="Warehouse Packer",
            description="Pick and pack customer orders.",
        ),
        _job_values(
            index=1,
            last_imported_at=BASE_TIME + timedelta(minutes=1),
            title="Senior Accountant",
            description="Manage reconciliations and reporting.",
        ),
    ]

    async def run() -> None:
        async with _api(test_database_url, rows) as client:
            # "reconciliation" stems to the lexeme stored for
            # "reconciliations" under the english config baked into the
            # generated column.
            response = await client.get(
                "/api/v1/jobs", params={"q": "reconciliation", "limit": 100}
            )
            assert response.status_code == 200
            assert [item["slug"] for item in response.json()["items"]] == ["test-job-1"]

            # websearch_to_tsquery operator syntax must not raise a 500.
            noisy = await client.get(
                "/api/v1/jobs", params={"q": '"warehouse packer" -accountant'}
            )
            assert noisy.status_code == 200
            assert [item["slug"] for item in noisy.json()["items"]] == ["test-job-0"]

    _run(run)


@pytest.mark.parametrize(
    "cursor",
    [
        "not-base64-!!!",
        base64.urlsafe_b64encode(b"not json at all").decode(),
        base64.urlsafe_b64encode(json.dumps([1, 2]).encode()).decode(),
        _encode_cursor_payload({"id": 5, "served": 0}),
        _encode_cursor_payload({"v": "yesterday", "id": 5, "served": 0}),
        _encode_cursor_payload({"v": BASE_TIME.isoformat(), "id": "five", "served": 0}),
    ],
)
def test_malformed_cursor_returns_400_not_500(
    test_database_url: str, cursor: str
) -> None:
    async def run() -> None:
        async with _api(test_database_url, []) as client:
            response = await client.get("/api/v1/jobs", params={"cursor": cursor})
            assert response.status_code == 400
            assert "Invalid cursor" in response.json()["detail"]

    _run(run)


def test_cursor_with_negative_served_count_returns_400(
    test_database_url: str,
) -> None:
    cursor = _encode_cursor_payload({"v": BASE_TIME.isoformat(), "id": 5, "served": -1})

    async def run() -> None:
        async with _api(test_database_url, []) as client:
            response = await client.get("/api/v1/jobs", params={"cursor": cursor})
            assert response.status_code == 400
            assert "served" in response.json()["detail"]

    _run(run)


def test_cursor_with_non_integer_served_count_returns_400(
    test_database_url: str,
) -> None:
    cursor = _encode_cursor_payload(
        {"v": BASE_TIME.isoformat(), "id": 5, "served": "9999"}
    )

    async def run() -> None:
        async with _api(test_database_url, []) as client:
            response = await client.get("/api/v1/jobs", params={"cursor": cursor})
            assert response.status_code == 400
            assert "served" in response.json()["detail"]

    _run(run)


def test_legacy_cursor_without_served_count_returns_400(
    test_database_url: str,
) -> None:
    cursor = _encode_cursor_payload({"v": BASE_TIME.isoformat(), "id": 5})

    async def run() -> None:
        async with _api(test_database_url, []) as client:
            response = await client.get("/api/v1/jobs", params={"cursor": cursor})
            assert response.status_code == 400
            assert "served" in response.json()["detail"]

    _run(run)


def test_limit_and_sort_bounds_are_enforced(test_database_url: str) -> None:
    async def run() -> None:
        async with _api(test_database_url, []) as client:
            too_large = await client.get("/api/v1/jobs", params={"limit": 101})
            assert too_large.status_code == 422
            too_small = await client.get("/api/v1/jobs", params={"limit": 0})
            assert too_small.status_code == 422
            bad_sort = await client.get("/api/v1/jobs", params={"sort": "title"})
            assert bad_sort.status_code == 422

    _run(run)


def test_response_never_leaks_internal_columns(test_database_url: str) -> None:
    rows = [_job_values(index=0, last_imported_at=BASE_TIME)]

    async def run() -> None:
        async with _api(test_database_url, rows) as client:
            response = await client.get("/api/v1/jobs")
            item = response.json()["items"][0]
            assert set(item) == {
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
            }
            assert item["remote_status"] == rows[0]["remote_status"]
            assert item["remote_status_source"] == rows[0]["remote_status_source"]
            assert item["experience_level"] == rows[0]["experience_level"]
            assert item["experience_level_source"] == rows[0]["experience_level_source"]

    _run(run)
