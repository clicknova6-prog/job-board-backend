"""Request and response contracts for provider administration."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class ProviderRead(BaseModel):
    """Complete provider configuration exposed to trusted administrators."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    feed_url: str | None
    format: str
    archive_type: str | None
    schedule_cron: str | None
    timeout_seconds: int | None
    retry_max_attempts: int
    is_active: bool
    config: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    schedule_interval_minutes: int | None
    deleted_job_retention_hours: int | None


class ProviderUpdate(BaseModel):
    """Fields that may be changed on an existing provider."""

    model_config = ConfigDict(extra="forbid")

    feed_url: str | None = None
    format: str | None = None
    archive_type: str | None = None
    schedule_cron: str | None = None
    timeout_seconds: int | None = None
    retry_max_attempts: int | None = None
    config: dict[str, Any] | None = None
    schedule_interval_minutes: int | None = None
    deleted_job_retention_hours: int | None = None
