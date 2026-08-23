"""Async read-side persistence for administrator import history views."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ImportRun, JobStaging
from app.db.repositories import _mask_sensitive_query_parameters


@dataclass(frozen=True, slots=True)
class ImportRunRecord:
    """Import-run data exposed outside the repository layer."""

    id: int
    provider_id: int
    source_name: str
    source_uri: str | None
    status: str
    started_at: datetime | None
    completed_at: datetime | None
    records_received: int
    records_staged: int
    records_imported: int
    records_rejected: int
    new_jobs: int
    updated_jobs: int
    deleted_jobs: int
    error_message: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class RejectedRecordRow:
    """Lightweight rejected staging record for an administrator list view."""

    id: int
    import_run_id: int
    source_job_id: str
    title: str | None
    validation_errors: list[dict[str, Any]]
    staged_at: datetime


def _import_run_record(row: RowMapping) -> ImportRunRecord:
    """Copy a projected row and mask credential-like source URI parameters."""
    values = dict(row)
    values["source_uri"] = _mask_sensitive_query_parameters(values["source_uri"])
    return ImportRunRecord(**values)


class ImportRunRepository:
    """Read import-run history and rejection details for administrators."""

    def __init__(self, session: AsyncSession) -> None:
        """Store the async SQLAlchemy session."""
        self._session = session

    async def list_import_runs(
        self,
        provider_id: int | None,
        status: str | None,
        limit: int,
        offset: int,
    ) -> list[ImportRunRecord]:
        """Return a filtered page of newest-first import runs."""
        statement = (
            self._apply_import_run_filters(
                self._import_run_columns(),
                provider_id,
                status,
            )
            .order_by(ImportRun.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self._session.execute(statement)
        return [_import_run_record(row) for row in result.mappings()]

    async def count_import_runs(
        self,
        provider_id: int | None,
        status: str | None,
    ) -> int:
        """Count import runs matching the optional provider and status filters."""
        statement = self._apply_import_run_filters(
            select(func.count()).select_from(ImportRun),
            provider_id,
            status,
        )
        return int((await self._session.execute(statement)).scalar_one())

    async def get_import_run(self, import_run_id: int) -> ImportRunRecord | None:
        """Return one import run by primary key."""
        result = await self._session.execute(
            self._import_run_columns().where(ImportRun.id == import_run_id)
        )
        row = result.mappings().one_or_none()
        return None if row is None else _import_run_record(row)

    async def list_rejected_records(
        self,
        import_run_id: int,
        limit: int,
        offset: int,
    ) -> list[RejectedRecordRow]:
        """Return a page of rejected staging rows in stable ID order."""
        result = await self._session.execute(
            select(
                JobStaging.id,
                JobStaging.import_run_id,
                JobStaging.source_job_id,
                JobStaging.title,
                JobStaging.validation_errors,
                JobStaging.staged_at,
            )
            .where(
                JobStaging.import_run_id == import_run_id,
                JobStaging.is_valid.is_(False),
            )
            .order_by(JobStaging.id.asc())
            .limit(limit)
            .offset(offset)
        )
        return [RejectedRecordRow(**row) for row in result.mappings()]

    async def count_rejected_records(self, import_run_id: int) -> int:
        """Count rejected staging rows belonging to one import run."""
        result = await self._session.execute(
            select(func.count())
            .select_from(JobStaging)
            .where(
                JobStaging.import_run_id == import_run_id,
                JobStaging.is_valid.is_(False),
            )
        )
        return int(result.scalar_one())

    @staticmethod
    def _import_run_columns() -> Select[Any]:
        """Build the common explicit import-run column projection."""
        return select(
            ImportRun.id,
            ImportRun.provider_id,
            ImportRun.source_name,
            ImportRun.source_uri,
            ImportRun.status,
            ImportRun.started_at,
            ImportRun.completed_at,
            ImportRun.records_received,
            ImportRun.records_staged,
            ImportRun.records_imported,
            ImportRun.records_rejected,
            ImportRun.new_jobs,
            ImportRun.updated_jobs,
            ImportRun.deleted_jobs,
            ImportRun.error_message,
            ImportRun.created_at,
            ImportRun.updated_at,
        )

    @staticmethod
    def _apply_import_run_filters(
        statement: Select[Any],
        provider_id: int | None,
        status: str | None,
    ) -> Select[Any]:
        """Apply optional import-run list and count filters."""
        if provider_id is not None:
            statement = statement.where(ImportRun.provider_id == provider_id)
        if status is not None:
            statement = statement.where(ImportRun.status == status)
        return statement
