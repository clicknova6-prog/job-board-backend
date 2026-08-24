"""Response contracts for administrator import history and health views."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class ImportRunRead(BaseModel):
    """One import-run history record exposed to administrators."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    provider_id: int
    source_name: str
    source_uri: str | None
    source_checksum: str | None
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
    unmapped_fields: dict[str, int]
    field_fallback_warnings: dict[str, int]
    error_message: str | None
    created_at: datetime
    updated_at: datetime


class RejectedRecordRead(BaseModel):
    """One rejected staging record exposed to administrators."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    import_run_id: int
    source_job_id: str
    title: str | None
    validation_errors: list[dict[str, Any]]
    staged_at: datetime


class PaginatedImportRuns(BaseModel):
    """An offset-paginated page of import-run records."""

    items: list[ImportRunRead]
    total: int
    limit: int
    offset: int


class PaginatedRejectedRecords(BaseModel):
    """An offset-paginated page of rejected staging records."""

    items: list[RejectedRecordRead]
    total: int
    limit: int
    offset: int
