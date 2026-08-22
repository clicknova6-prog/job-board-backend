"""Application service for hard-deleting expired inactive jobs."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.db.repositories import CleanupRepository
from app.db.session import SessionLocal

logger = logging.getLogger(__name__)

DEFAULT_RETENTION_HOURS = 12


@dataclass(frozen=True, slots=True)
class ProviderCleanupSummary:
    """Hard-delete counts for one provider."""

    provider_id: int
    provider_name: str
    retention_hours: int
    checked_jobs: int
    deleted_jobs: int


@dataclass(frozen=True, slots=True)
class CleanupSummary:
    """Aggregate and per-provider hard-delete counts."""

    checked_jobs: int
    deleted_jobs: int
    providers: list[ProviderCleanupSummary]


class CleanupService:
    """Hard-delete inactive jobs after each provider's retention window."""

    def __init__(
        self,
        *,
        batch_size: int = 1000,
        service_logger: logging.Logger | None = None,
    ) -> None:
        """Configure cleanup using the same batch size as promotion."""
        if batch_size <= 0:
            raise ValueError("batch_size must be greater than zero")

        self.batch_size = batch_size
        self.logger = service_logger or logger

    def run(self) -> CleanupSummary:
        """Delete all currently expired jobs and return cleanup counts."""
        now = datetime.now(tz=UTC)
        provider_summaries: list[ProviderCleanupSummary] = []

        with SessionLocal() as session:
            repo = CleanupRepository(session)
            try:
                policies = repo.list_provider_retention_policies()
                for policy in policies:
                    retention_hours = (
                        policy.retention_hours
                        if policy.retention_hours is not None
                        else DEFAULT_RETENTION_HOURS
                    )
                    cutoff = now - timedelta(hours=retention_hours)
                    checked_jobs = 0
                    deleted_jobs = 0

                    while job_ids := repo.find_expired_job_ids(
                        provider_id=policy.provider_id,
                        cutoff=cutoff,
                        limit=self.batch_size,
                    ):
                        checked_jobs += len(job_ids)
                        deleted_in_batch = repo.hard_delete_expired_jobs(
                            job_ids=job_ids,
                            provider_id=policy.provider_id,
                            cutoff=cutoff,
                        )
                        deleted_jobs += deleted_in_batch
                        repo.commit()
                        self.logger.info(
                            "Expired job cleanup batch committed",
                            extra={
                                "provider_id": policy.provider_id,
                                "provider_name": policy.provider_name,
                                "checked_jobs": len(job_ids),
                                "deleted_jobs": deleted_in_batch,
                            },
                        )

                    provider_summary = ProviderCleanupSummary(
                        provider_id=policy.provider_id,
                        provider_name=policy.provider_name,
                        retention_hours=retention_hours,
                        checked_jobs=checked_jobs,
                        deleted_jobs=deleted_jobs,
                    )
                    provider_summaries.append(provider_summary)
                    self.logger.info(
                        "Expired job cleanup completed for provider",
                        extra={
                            "provider_id": provider_summary.provider_id,
                            "provider_name": provider_summary.provider_name,
                            "retention_hours": provider_summary.retention_hours,
                            "checked_jobs": provider_summary.checked_jobs,
                            "deleted_jobs": provider_summary.deleted_jobs,
                        },
                    )
            except Exception:
                repo.rollback()
                raise

        summary = CleanupSummary(
            checked_jobs=sum(item.checked_jobs for item in provider_summaries),
            deleted_jobs=sum(item.deleted_jobs for item in provider_summaries),
            providers=provider_summaries,
        )
        self.logger.info(
            "Expired job cleanup completed",
            extra={
                "checked_jobs": summary.checked_jobs,
                "deleted_jobs": summary.deleted_jobs,
                "provider_count": len(summary.providers),
            },
        )
        return summary
