"""Application service for validating and staging a streamed Jobg8 feed."""

import logging
from dataclasses import dataclass
from pathlib import Path
from app.imports.hashing import compute_payload_hash
from pydantic import ValidationError

from app.db.repositories import JobRepository
from app.db.session import SessionLocal
from app.imports.parser import parse_job_feed
from app.imports.schemas import JobFeedRecord


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ImportSummary:
    """Counts collected while processing one feed snapshot."""

    import_run_id: int
    total_jobs: int
    valid_jobs: int
    invalid_jobs: int


class ImportService:
    """Stream, validate, and stage a single XML feed file.

    Validated records are written to the job_staging table inside a single
    database transaction. The transaction is committed once after the full
    feed is processed, or rolled back if any unhandled error occurs.
    """

    def __init__(
        self,
        feed_path: str | Path,
        *,
        progress_interval: int = 25_000,
        service_logger: logging.Logger | None = None,
    ) -> None:
        """Configure an import for one XML file path."""
        if progress_interval <= 0:
            raise ValueError("progress_interval must be greater than zero")

        self.feed_path = Path(feed_path)
        self.progress_interval = progress_interval
        self.logger = service_logger or logger

    def run(self) -> ImportSummary:
        """Validate and stage every job in the feed, then return final counts.

        XML syntax and file errors still stop the import because the remaining
        document cannot be trusted. Individual Pydantic validation errors are
        logged and counted, allowing the rest of a valid XML snapshot to run.

        A single database transaction spans the entire feed. On success it is
        committed once at the end. On any unhandled exception the transaction
        is rolled back and the original exception is re-raised.
        """
        total_jobs = 0
        valid_jobs = 0
        invalid_jobs = 0
        import_run_id: int
        # Unknown XML element name -> number of records it appeared in. The
        # schema allows extras rather than rejecting them, so this is the only
        # signal that the feed has grown a field the schema does not map.
        unmapped_fields: dict[str, int] = {}
        # Field name -> number of records whose raw value needed fallback
        # handling. Rising counts mean the feed's real format is drifting away
        # from what the schema expects, even though no record was rejected.
        field_fallback_warnings: dict[str, int] = {}

        with SessionLocal() as session:
            repo = JobRepository(session)

            try:
                run = repo.create_import_run(
                    source_name="jobg8",
                    source_uri=str(self.feed_path),
                )
                # Flush so that run.id is populated from the Identity sequence
                # before we pass the run object to stage_job().
                repo.flush()

                # Capture the id while the session is still open so callers can
                # hand it to the promotion phase without re-querying.
                import_run_id = run.id

                def handle_validation_error(
                    raw_record: dict[str, str | None],
                    error: ValidationError,
                ) -> None:
                    """Count and log one invalid XML record without retaining it."""
                    nonlocal total_jobs, invalid_jobs
                    total_jobs += 1
                    invalid_jobs += 1
                    self._log_progress(total_jobs, valid_jobs, invalid_jobs)

                    # SenderReference may itself be missing or invalid, so use a
                    # clear fallback instead of assuming it can be read from a
                    # Pydantic model.
                    sender_reference = raw_record.get("SenderReference") or "<missing>"
                    self.logger.warning(
                        "Invalid job record: sender_reference=%s validation_error=%s",
                        sender_reference,
                        error,
                    )

                job: JobFeedRecord
                for job in parse_job_feed(
                    self.feed_path,
                    on_validation_error=handle_validation_error,
                ):
                    # Receiving a value from the parser means this Job XML element
                    # has passed all Pydantic validation rules.
                    total_jobs += 1
                    valid_jobs += 1
                    self._log_progress(total_jobs, valid_jobs, invalid_jobs)

                    # Count each unrecognized element once per record, not once
                    # per occurrence, so the totals stay comparable to
                    # records_received.
                    for unknown_field in job.model_extra or {}:
                        unmapped_fields[unknown_field] = (
                            unmapped_fields.get(unknown_field, 0) + 1
                        )

                    # fallback_fields is a per-record set, so this counts each
                    # field once per record rather than once per occurrence.
                    for fallback_field in job.fallback_fields:
                        field_fallback_warnings[fallback_field] = (
                            field_fallback_warnings.get(fallback_field, 0) + 1
                        )

                    # The hash is still computed over the normalized model, so
                    # update detection is unaffected by feed formatting churn.
                    # raw_payload instead keeps the provider's original record,
                    # including any unmapped elements, so source_payload stays a
                    # faithful copy of what was actually sent.
                    payload_hash = compute_payload_hash(job)
                    raw_payload = job.source_record

                    repo.stage_job(
                        run=run,
                        record=job,
                        payload_hash=payload_hash,
                        raw_payload=raw_payload,
                    )

                if unmapped_fields:
                    self.logger.warning(
                        "Feed contained unmapped fields: %s",
                        ", ".join(
                            f"{name}={count}"
                            for name, count in sorted(unmapped_fields.items())
                        ),
                    )
                repo.record_unmapped_fields(run=run, counts=unmapped_fields)

                if field_fallback_warnings:
                    self.logger.warning(
                        "Feed required field fallbacks: %s",
                        ", ".join(
                            f"{name}={count}"
                            for name, count in sorted(field_fallback_warnings.items())
                        ),
                    )
                repo.record_field_fallback_warnings(
                    run=run, counts=field_fallback_warnings
                )

                repo.finish_import(
                    run=run,
                    status="completed",
                    received=total_jobs,
                    staged=valid_jobs,
                    imported=0,
                    rejected=invalid_jobs,
                )
                repo.commit()

            except Exception:
                repo.rollback()
                raise

        return ImportSummary(
            import_run_id=import_run_id,
            total_jobs=total_jobs,
            valid_jobs=valid_jobs,
            invalid_jobs=invalid_jobs,
        )

    def _log_progress(
        self,
        total_jobs: int,
        valid_jobs: int,
        invalid_jobs: int,
    ) -> None:
        """Log only fixed-size milestones to keep large-feed logs useful."""
        if total_jobs % self.progress_interval == 0:
            self.logger.info(
                "Job feed progress: total=%d valid=%d invalid=%d",
                total_jobs,
                valid_jobs,
                invalid_jobs,
            )