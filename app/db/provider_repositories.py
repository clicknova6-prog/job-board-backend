"""Async persistence operations for provider administration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Provider


@dataclass(frozen=True, slots=True)
class ProviderRecord:
    """Provider data exposed outside the repository layer."""

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


def _provider_record(provider: Provider) -> ProviderRecord:
    """Copy a Provider ORM object into an immutable route-layer record."""
    return ProviderRecord(
        id=provider.id,
        name=provider.name,
        feed_url=provider.feed_url,
        format=provider.format,
        archive_type=provider.archive_type,
        schedule_cron=provider.schedule_cron,
        timeout_seconds=provider.timeout_seconds,
        retry_max_attempts=provider.retry_max_attempts,
        is_active=provider.is_active,
        config=provider.config,
        created_at=provider.created_at,
        updated_at=provider.updated_at,
        schedule_interval_minutes=provider.schedule_interval_minutes,
        deleted_job_retention_hours=provider.deleted_job_retention_hours,
    )


class ProviderRepository:
    """All ORM access required by provider administration routes."""

    def __init__(self, session: AsyncSession) -> None:
        """Store the async SQLAlchemy session."""
        self._session = session

    async def list_providers(self) -> list[ProviderRecord]:
        """Return every configured provider in stable ID order."""
        providers = (
            await self._session.scalars(select(Provider).order_by(Provider.id))
        ).all()
        return [_provider_record(provider) for provider in providers]

    async def get_provider(self, provider_id: int) -> ProviderRecord | None:
        """Return one provider by primary key."""
        provider = await self._session.get(Provider, provider_id)
        return _provider_record(provider) if provider is not None else None

    async def update_provider(
        self,
        provider_id: int,
        **fields: Any,
    ) -> tuple[ProviderRecord, ProviderRecord] | None:
        """Apply fields and return immutable before/after states without committing."""
        provider = await self._get_for_update(provider_id)
        if provider is None:
            return None
        before = _provider_record(provider)
        for field, value in fields.items():
            setattr(provider, field, value)
        provider.updated_at = datetime.now(UTC)
        await self._session.flush()
        return before, _provider_record(provider)

    async def set_provider_active(
        self,
        provider_id: int,
        is_active: bool,
    ) -> tuple[ProviderRecord, ProviderRecord] | None:
        """Set provider activation and return states without committing."""
        return await self.update_provider(provider_id, is_active=is_active)

    async def _get_for_update(self, provider_id: int) -> Provider | None:
        """Lock a provider row for a transactional mutation."""
        return await self._session.scalar(
            select(Provider).where(Provider.id == provider_id).with_for_update()
        )
