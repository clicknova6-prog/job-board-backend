"""Integration tests for authenticated profile and saved-job routes."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Coroutine, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import delete, func, insert, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.auth.dependencies import get_current_user
from app.auth.schemas import CurrentUser
from app.db.async_session import get_async_session
from app.db.models import Job, OAuthProvider, Provider, SavedJob, User
from app.main import app

BASE_TIME = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)


def _async_url(sync_url: str) -> str:
    return sync_url.replace("postgresql://", "postgresql+asyncpg://", 1)


def _user_values(*, user_id: UUID, index: int) -> dict[str, Any]:
    return {
        "id": user_id,
        "email": f"user-{index}@example.test",
        "oauth_provider": OAuthProvider.GOOGLE,
        "oauth_subject_id": f"google-user-{index}",
        "display_name": f"User {index}",
    }


def _job_values(*, index: int, is_active: bool = True) -> dict[str, Any]:
    return {
        "source_name": "jobg8",
        "source_job_id": f"saved-job-{index}",
        "slug": f"saved-job-{index}",
        "title": f"Saved Job {index}",
        "description": f"Description for saved job {index}.",
        "classification": "Information Technology",
        "employment_type": "Full Time",
        "country_name": "Australia",
        "location": "Sydney",
        "apply_url": f"https://example.test/apply/{index}",
        "source_payload": {"SenderReference": f"saved-job-{index}"},
        "payload_hash": f"saved-job-hash-{index}",
        "is_active": is_active,
        "deactivated_at": None if is_active else BASE_TIME,
        "first_imported_at": BASE_TIME - timedelta(days=1),
        "last_imported_at": BASE_TIME + timedelta(minutes=index),
        "remote_status": "remote",
        "remote_status_source": "inferred",
        "experience_level": "senior",
        "experience_level_source": "inferred",
    }


@asynccontextmanager
async def _api(
    database_url: str,
    *,
    users: Sequence[dict[str, Any]] = (),
    jobs: Sequence[dict[str, Any]] = (),
    saved_jobs: Sequence[tuple[UUID, str, datetime]] = (),
    current_user_id: UUID | None = None,
) -> AsyncIterator[
    tuple[
        httpx.AsyncClient,
        async_sessionmaker[AsyncSession],
        dict[str, int],
    ]
]:
    engine = create_async_engine(_async_url(database_url), poolclass=NullPool)
    session_factory = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    job_ids: dict[str, int] = {}

    try:
        async with session_factory() as setup_session:
            await setup_session.execute(delete(SavedJob))
            await setup_session.execute(delete(Job))
            await setup_session.execute(delete(User))

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

            if users:
                await setup_session.execute(insert(User), list(users))
            for job in jobs:
                job_id = (
                    await setup_session.execute(
                        insert(Job)
                        .values(**job, provider_id=provider_id)
                        .returning(Job.id)
                    )
                ).scalar_one()
                job_ids[job["source_job_id"]] = job_id
            for user_id, source_job_id, saved_at in saved_jobs:
                await setup_session.execute(
                    insert(SavedJob).values(
                        user_id=user_id,
                        job_id=job_ids[source_job_id],
                        saved_at=saved_at,
                    )
                )
            await setup_session.commit()

        async def override_session() -> AsyncIterator[AsyncSession]:
            async with session_factory() as session:
                yield session

        app.dependency_overrides[get_async_session] = override_session
        if current_user_id is not None:

            async def override_current_user() -> CurrentUser:
                return CurrentUser(id=current_user_id)

            app.dependency_overrides[get_current_user] = override_current_user

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            yield client, session_factory, job_ids
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_async_session, None)
        await engine.dispose()


def _run(coroutine_factory: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(coroutine_factory())


@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        ("GET", "/api/v1/me", {}),
        ("PATCH", "/api/v1/me", {"json": {"display_name": "New Name"}}),
        ("GET", "/api/v1/me/saved-jobs", {}),
        ("POST", "/api/v1/me/saved-jobs/1", {}),
        ("DELETE", "/api/v1/me/saved-jobs/1", {}),
    ],
)
def test_me_routes_require_authentication(
    test_database_url: str,
    method: str,
    path: str,
    kwargs: dict[str, Any],
) -> None:
    async def run() -> None:
        async with _api(test_database_url) as (client, _, __):
            response = await client.request(method, path, **kwargs)
            assert response.status_code == 401

    _run(run)


def test_profile_read_and_update(test_database_url: str) -> None:
    user_id = uuid4()
    user = _user_values(user_id=user_id, index=1)

    async def run() -> None:
        async with _api(
            test_database_url,
            users=[user],
            current_user_id=user_id,
        ) as (client, _, __):
            read = await client.get("/api/v1/me")
            assert read.status_code == 200
            assert read.json() == {
                "id": str(user_id),
                "email": user["email"],
                "display_name": user["display_name"],
            }

            updated = await client.patch(
                "/api/v1/me",
                json={"display_name": "  Updated User  "},
            )
            assert updated.status_code == 200
            assert updated.json() == {
                "id": str(user_id),
                "email": user["email"],
                "display_name": "Updated User",
            }

            reread = await client.get("/api/v1/me")
            assert reread.json()["display_name"] == "Updated User"

    _run(run)


def test_user_cannot_see_or_modify_another_users_data(
    test_database_url: str,
) -> None:
    user_a_id = uuid4()
    user_b_id = uuid4()
    job = _job_values(index=1)

    async def run() -> None:
        async with _api(
            test_database_url,
            users=[
                _user_values(user_id=user_a_id, index=1),
                _user_values(user_id=user_b_id, index=2),
            ],
            jobs=[job],
            saved_jobs=[(user_b_id, job["source_job_id"], BASE_TIME)],
            current_user_id=user_a_id,
        ) as (client, session_factory, job_ids):
            listed = await client.get("/api/v1/me/saved-jobs")
            assert listed.status_code == 200
            assert listed.json() == []

            deleted = await client.delete(
                f"/api/v1/me/saved-jobs/{job_ids[job['source_job_id']]}"
            )
            assert deleted.status_code == 204

            updated = await client.patch(
                "/api/v1/me",
                json={"display_name": "Updated A"},
            )
            assert updated.status_code == 200

            async with session_factory() as session:
                names = dict(
                    (
                        await session.execute(
                            select(User.id, User.display_name).where(
                                User.id.in_([user_a_id, user_b_id])
                            )
                        )
                    ).all()
                )
                saved_count = await session.scalar(
                    select(func.count()).select_from(SavedJob)
                )
            assert names == {user_a_id: "Updated A", user_b_id: "User 2"}
            assert saved_count == 1

    _run(run)


def test_saved_jobs_are_ordered_most_recently_saved_first(
    test_database_url: str,
) -> None:
    user_id = uuid4()
    jobs = [_job_values(index=index) for index in range(3)]

    async def run() -> None:
        async with _api(
            test_database_url,
            users=[_user_values(user_id=user_id, index=1)],
            jobs=jobs,
            saved_jobs=[
                (user_id, jobs[0]["source_job_id"], BASE_TIME),
                (user_id, jobs[1]["source_job_id"], BASE_TIME + timedelta(hours=2)),
                (user_id, jobs[2]["source_job_id"], BASE_TIME + timedelta(hours=1)),
            ],
            current_user_id=user_id,
        ) as (client, _, __):
            response = await client.get("/api/v1/me/saved-jobs")
            assert response.status_code == 200
            assert [item["slug"] for item in response.json()] == [
                "saved-job-1",
                "saved-job-2",
                "saved-job-0",
            ]

    _run(run)


def test_saving_a_job_is_idempotent(test_database_url: str) -> None:
    user_id = uuid4()
    job = _job_values(index=1)

    async def run() -> None:
        async with _api(
            test_database_url,
            users=[_user_values(user_id=user_id, index=1)],
            jobs=[job],
            current_user_id=user_id,
        ) as (client, session_factory, job_ids):
            path = f"/api/v1/me/saved-jobs/{job_ids[job['source_job_id']]}"
            first = await client.post(path)
            second = await client.post(path)

            assert first.status_code == 200
            assert second.status_code == 200
            assert second.json() == first.json()

            async with session_factory() as session:
                saved_count = await session.scalar(
                    select(func.count()).select_from(SavedJob)
                )
            assert saved_count == 1

    _run(run)


def test_deleting_a_saved_job_is_idempotent(test_database_url: str) -> None:
    user_id = uuid4()
    job = _job_values(index=1)

    async def run() -> None:
        async with _api(
            test_database_url,
            users=[_user_values(user_id=user_id, index=1)],
            jobs=[job],
            saved_jobs=[(user_id, job["source_job_id"], BASE_TIME)],
            current_user_id=user_id,
        ) as (client, session_factory, job_ids):
            path = f"/api/v1/me/saved-jobs/{job_ids[job['source_job_id']]}"
            first = await client.delete(path)
            second = await client.delete(path)

            assert first.status_code == 204
            assert second.status_code == 204
            assert first.content == b""
            assert second.content == b""

            async with session_factory() as session:
                saved_count = await session.scalar(
                    select(func.count()).select_from(SavedJob)
                )
            assert saved_count == 0

    _run(run)


def test_saving_a_nonexistent_job_returns_404(test_database_url: str) -> None:
    user_id = uuid4()

    async def run() -> None:
        async with _api(
            test_database_url,
            users=[_user_values(user_id=user_id, index=1)],
            current_user_id=user_id,
        ) as (client, _, __):
            response = await client.post("/api/v1/me/saved-jobs/999999999")
            assert response.status_code == 404
            assert response.json() == {
                "error": {
                    "code": "NOT_FOUND",
                    "message": "Job not found",
                    "details": None,
                }
            }

    _run(run)


def test_saving_an_inactive_job_returns_404(test_database_url: str) -> None:
    user_id = uuid4()
    job = _job_values(index=1, is_active=False)

    async def run() -> None:
        async with _api(
            test_database_url,
            users=[_user_values(user_id=user_id, index=1)],
            jobs=[job],
            current_user_id=user_id,
        ) as (client, session_factory, job_ids):
            response = await client.post(
                f"/api/v1/me/saved-jobs/{job_ids[job['source_job_id']]}"
            )
            assert response.status_code == 404
            assert response.json() == {
                "error": {
                    "code": "NOT_FOUND",
                    "message": "Job not found",
                    "details": None,
                }
            }

            async with session_factory() as session:
                saved_count = await session.scalar(
                    select(func.count()).select_from(SavedJob)
                )
            assert saved_count == 0

    _run(run)
