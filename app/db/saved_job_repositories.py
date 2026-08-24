"""Async persistence for job-seeker profiles and saved jobs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Job, SavedJob, User
from app.db.public_job_repositories import JobSummaryRecord


@dataclass(frozen=True, slots=True)
class UserProfileRecord:
    """Publishable profile fields handed out of the repository layer."""

    id: UUID
    email: str
    display_name: str | None


@dataclass(frozen=True, slots=True)
class SavedJobRecord:
    """One persisted job bookmark handed out of the repository layer."""

    id: UUID
    job_id: int
    saved_at: datetime


class SavedJobRepository:
    """Read and mutate one job seeker's profile and saved jobs."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_user_profile(self, user_id: UUID) -> UserProfileRecord | None:
        """Return the active job seeker's public profile."""
        result = await self._session.execute(
            select(User.id, User.email, User.display_name).where(
                User.id == user_id,
                User.deleted_at.is_(None),
            )
        )
        row = result.mappings().one_or_none()
        return None if row is None else UserProfileRecord(**row)

    async def update_display_name(
        self,
        user_id: UUID,
        display_name: str,
    ) -> UserProfileRecord | None:
        """Update and return the active job seeker's public profile."""
        result = await self._session.execute(
            update(User)
            .where(
                User.id == user_id,
                User.deleted_at.is_(None),
            )
            .values(display_name=display_name, updated_at=func.now())
            .returning(User.id, User.email, User.display_name)
        )
        row = result.mappings().one_or_none()
        return None if row is None else UserProfileRecord(**row)

    async def list_saved_jobs(self, user_id: UUID) -> list[JobSummaryRecord]:
        """Return active saved jobs, most recently saved first."""
        result = await self._session.execute(
            select(
                Job.id,
                Job.slug,
                Job.title,
                Job.classification,
                Job.employment_type,
                Job.country_name,
                Job.location,
                Job.apply_url,
                Job.last_imported_at,
                Job.remote_status,
                Job.remote_status_source,
                Job.experience_level,
                Job.experience_level_source,
            )
            .join(SavedJob, SavedJob.job_id == Job.id)
            .where(
                SavedJob.user_id == user_id,
                Job.is_active.is_(True),
            )
            .order_by(SavedJob.saved_at.desc(), SavedJob.id.desc())
        )
        return [JobSummaryRecord(**row) for row in result.mappings()]

    async def save_job(
        self,
        user_id: UUID,
        job_id: int,
    ) -> SavedJobRecord | None:
        """Idempotently save an active job, or return ``None`` if unavailable."""
        active_job_id = await self._session.scalar(
            select(Job.id).where(
                Job.id == job_id,
                Job.is_active.is_(True),
            )
        )
        if active_job_id is None:
            return None

        result = await self._session.execute(
            insert(SavedJob)
            .values(user_id=user_id, job_id=job_id)
            .on_conflict_do_nothing(constraint="saved_jobs_user_job_unique")
            .returning(SavedJob.id, SavedJob.job_id, SavedJob.saved_at)
        )
        row = result.mappings().one_or_none()
        if row is None:
            result = await self._session.execute(
                select(SavedJob.id, SavedJob.job_id, SavedJob.saved_at).where(
                    SavedJob.user_id == user_id,
                    SavedJob.job_id == job_id,
                )
            )
            row = result.mappings().one()
        return SavedJobRecord(**row)

    async def delete_saved_job(self, user_id: UUID, job_id: int) -> None:
        """Delete one bookmark if present."""
        await self._session.execute(
            delete(SavedJob).where(
                SavedJob.user_id == user_id,
                SavedJob.job_id == job_id,
            )
        )
