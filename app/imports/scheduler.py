"""Scheduling decisions for provider feed imports."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.db.repositories import SchedulerRepository


@dataclass(frozen=True, slots=True)
class ProviderDispatchPlan:
    """Provider IDs grouped by the dispatch decision made for each one."""

    checked_provider_ids: list[int]
    due_provider_ids: list[int]
    skipped_not_due_provider_ids: list[int]
    skipped_processing_provider_ids: list[int]
    skipped_unconfigured_provider_ids: list[int]


class ProviderSchedulerService:
    """Determine which active providers are due for an import."""

    def __init__(self, repository: SchedulerRepository) -> None:
        """Use repository-provided scheduling state for all decisions."""
        self._repository = repository

    def build_dispatch_plan(
        self, *, now: datetime | None = None
    ) -> ProviderDispatchPlan:
        """Classify every active provider without enqueueing any tasks."""
        checked_provider_ids: list[int] = []
        due_provider_ids: list[int] = []
        skipped_not_due_provider_ids: list[int] = []
        skipped_processing_provider_ids: list[int] = []
        skipped_unconfigured_provider_ids: list[int] = []
        evaluated_at = now or datetime.now(tz=UTC)

        for provider in self._repository.list_active_provider_schedules():
            checked_provider_ids.append(provider.provider_id)

            if provider.has_processing_import:
                skipped_processing_provider_ids.append(provider.provider_id)
                continue

            if provider.schedule_interval_minutes is None:
                skipped_unconfigured_provider_ids.append(provider.provider_id)
                continue

            next_import_at = (
                provider.last_completed_at
                + timedelta(minutes=provider.schedule_interval_minutes)
                if provider.last_completed_at is not None
                else None
            )
            if next_import_at is not None and evaluated_at < next_import_at:
                skipped_not_due_provider_ids.append(provider.provider_id)
                continue

            due_provider_ids.append(provider.provider_id)

        return ProviderDispatchPlan(
            checked_provider_ids=checked_provider_ids,
            due_provider_ids=due_provider_ids,
            skipped_not_due_provider_ids=skipped_not_due_provider_ids,
            skipped_processing_provider_ids=skipped_processing_provider_ids,
            skipped_unconfigured_provider_ids=skipped_unconfigured_provider_ids,
        )
