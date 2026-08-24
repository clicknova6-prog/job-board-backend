"""Application service for promoting staged records into the canonical jobs table.

This runs after ImportService has staged and committed all records for an
import run. It is a separate phase: staging validates and lands raw records,
promotion decides what those records mean for the live jobs table.
"""

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.exc import OperationalError

from app.db.models import Job, JobStaging
from app.db.repositories import PromotionRepository
from app.db.session import SessionLocal
from app.imports.exceptions import TransientImportError

logger = logging.getLogger(__name__)

_SLUG_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def slugify(value: str) -> str:
    """Lowercase, hyphenate, and collapse a string into a URL-safe slug."""
    value = _SLUG_NON_ALNUM.sub("-", value.lower())
    return value.strip("-")


@dataclass(frozen=True, slots=True)
class PromotionSummary:
    """Counts collected while promoting one import run's staged records."""

    new_jobs: int
    updated_jobs: int
    unchanged_jobs: int
    deactivated_jobs: int


class PromotionService:
    """Promote is_valid staged records for one import run into `jobs`.

    Before any writes, an anomaly check compares this run's valid staged
    count against the provider's currently active job count, and its
    rejection rate against the provider's configured threshold. Either
    check tripping marks the run 'failed' and returns without touching the
    jobs table at all — existing live data is left completely untouched.

    Otherwise, staged rows are upserted into `jobs` in batches, each batch
    committed separately. After all batches land, jobs for this provider
    that this run did not see are soft-deleted (is_active=False).
    """

    def __init__(
        self,
        import_run_id: int,
        *,
        batch_size: int = 1000,
        service_logger: logging.Logger | None = None,
    ) -> None:
        """Configure a promotion pass for one already-staged import run."""
        if batch_size <= 0:
            raise ValueError("batch_size must be greater than zero")

        self.import_run_id = import_run_id
        self.batch_size = batch_size
        self.logger = service_logger or logger

    def run(self) -> PromotionSummary:
        """Run the anomaly check, then promote or abort, and return counts."""
        now = datetime.now(UTC)

        with SessionLocal() as session:
            repo = PromotionRepository(session)

            try:
                run = repo.get_import_run(self.import_run_id)
                if run is None:
                    raise ValueError(f"ImportRun {self.import_run_id} does not exist")

                provider = repo.get_provider(run.provider_id)
                if provider is None:
                    raise ValueError(f"Provider {run.provider_id} does not exist")

                staged_rows = repo.load_valid_staged_jobs(self.import_run_id)

                config = provider.config or {}
                drop_threshold_pct = config.get("anomaly_drop_threshold_pct", 20)
                rejection_rate_pct = config.get("anomaly_rejection_rate_pct", 15)
                active_count = repo.count_active_jobs(run.provider_id)
                valid_staged_count = len(staged_rows)

                anomaly_reasons, anomaly_reason_codes = self._anomaly_reasons(
                    active_count=active_count,
                    valid_staged_count=valid_staged_count,
                    drop_threshold_pct=drop_threshold_pct,
                    rejection_rate_pct=rejection_rate_pct,
                    records_received=run.records_received,
                    records_rejected=run.records_rejected,
                )
                if anomaly_reasons:
                    error_message = "Promotion aborted due to anomaly: " + "; ".join(
                        anomaly_reasons
                    )
                    self.logger.error(error_message)
                    repo.finish_promotion(
                        run,
                        status="failed",
                        records_imported=0,
                        error_message=error_message,
                        is_anomalous=True,
                        anomaly_reasons=anomaly_reason_codes,
                    )
                    repo.commit()
                    return PromotionSummary(
                        new_jobs=0, updated_jobs=0, unchanged_jobs=0, deactivated_jobs=0
                    )

                new_jobs = 0
                updated_jobs = 0
                unchanged_jobs = 0

                for start in range(0, len(staged_rows), self.batch_size):
                    batch = staged_rows[start : start + self.batch_size]
                    new_in_batch: list[tuple[Job, JobStaging]] = []

                    for staged in batch:
                        existing = repo.get_job_by_provider_and_source(
                            run.provider_id, staged.source_job_id
                        )
                        if existing is None:
                            # slug is assigned after flush, below — see create_job().
                            placeholder_slug = (
                                f"__pending__{run.provider_id}__{staged.source_job_id}"
                            )
                            job = repo.create_job(
                                run=run,
                                staged=staged,
                                placeholder_slug=placeholder_slug,
                                now=now,
                            )
                            new_in_batch.append((job, staged))
                            new_jobs += 1
                        elif existing.payload_hash != staged.payload_hash:
                            repo.update_job_from_staged(existing, staged, run.id, now)
                            updated_jobs += 1
                        else:
                            repo.mark_job_seen(existing, run.id, now)
                            unchanged_jobs += 1

                    if new_in_batch:
                        repo.flush()
                        for job, staged in new_in_batch:
                            slug_base = slugify(
                                f"{staged.title or ''}-{staged.advertiser_name or ''}"
                            )
                            job.slug = f"{slug_base}-{job.id}"

                    repo.commit()
                    self.logger.info(
                        "Promotion batch committed: run_id=%d batch=%d new=%d updated=%d unchanged=%d",
                        run.id,
                        len(batch),
                        new_jobs,
                        updated_jobs,
                        unchanged_jobs,
                    )

                deactivated_jobs = repo.deactivate_stale_jobs(
                    run.provider_id, run.id, now
                )
                repo.finish_promotion(
                    run,
                    status="completed",
                    records_imported=new_jobs + updated_jobs,
                    new_jobs=new_jobs,
                    updated_jobs=updated_jobs,
                    deleted_jobs=deactivated_jobs,
                    error_message=None,
                )
                repo.commit()

            except OperationalError as error:
                repo.rollback()
                self._mark_failed(error)
                raise TransientImportError(
                    f"Temporary database failure during promotion: {error}",
                    import_run_id=self.import_run_id,
                ) from error
            except Exception as error:
                repo.rollback()
                self._mark_failed(error)
                raise

        return PromotionSummary(
            new_jobs=new_jobs,
            updated_jobs=updated_jobs,
            unchanged_jobs=unchanged_jobs,
            deactivated_jobs=deactivated_jobs,
        )

    def _mark_failed(self, error: BaseException) -> None:
        """Persist promotion failure after its working transaction rolls back."""
        try:
            with SessionLocal() as session:
                repo = PromotionRepository(session)
                run = repo.get_import_run(self.import_run_id)
                if run is None:
                    self.logger.error(
                        "Cannot mark missing import run as failed",
                        extra={"import_run_id": self.import_run_id},
                    )
                    return
                repo.finish_promotion(
                    run,
                    status="failed",
                    records_imported=run.records_imported,
                    new_jobs=run.new_jobs,
                    updated_jobs=run.updated_jobs,
                    deleted_jobs=run.deleted_jobs,
                    error_message=str(error),
                )
                repo.commit()
        except Exception:
            self.logger.exception(
                "Could not persist failed promotion outcome",
                extra={"import_run_id": self.import_run_id},
            )

    @staticmethod
    def _anomaly_reasons(
        *,
        active_count: int,
        valid_staged_count: int,
        drop_threshold_pct: float,
        rejection_rate_pct: float,
        records_received: int,
        records_rejected: int,
    ) -> tuple[list[str], list[str]]:
        """Return human-readable reasons and machine-readable reason codes.

        A drop can only be measured against a provider that already has
        active jobs, and a rejection rate only against a run that received
        at least one record — both checks are skipped otherwise rather than
        dividing by zero.
        """
        reasons: list[str] = []
        reason_codes: list[str] = []

        if active_count > 0:
            drop_pct = (active_count - valid_staged_count) / active_count * 100
            if drop_pct > drop_threshold_pct:
                reason_codes.append("catalogue_drop")
                reasons.append(
                    f"active job count dropped {drop_pct:.1f}% "
                    f"(active={active_count}, valid_staged={valid_staged_count}), "
                    f"exceeding anomaly_drop_threshold_pct={drop_threshold_pct}"
                )

        if records_received > 0:
            rejection_rate = (records_rejected / records_received) * 100
            if rejection_rate > rejection_rate_pct:
                reason_codes.append("high_rejection_rate")
                reasons.append(
                    f"rejection rate {rejection_rate:.1f}% "
                    f"(rejected={records_rejected}, received={records_received}), "
                    f"exceeding anomaly_rejection_rate_pct={rejection_rate_pct}"
                )

        return reasons, reason_codes
