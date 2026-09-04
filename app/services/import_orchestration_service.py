"""Orchestration for a single provider feed import run.

The Celery task in ``app.tasks.import_tasks`` is a thin wrapper around this
service: everything that is not Celery mechanics — session lifetime, service
sequencing, error classification, run-status reconciliation and logging —
lives here.
"""

from __future__ import annotations

import logging
from time import perf_counter

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.db.repositories import PromotionRepository
from app.db.session import SessionLocal
from app.imports.downloader import DownloadService
from app.imports.exceptions import TransientImportError
from app.imports.importer import ImportService
from app.imports.promotion import PromotionService

logger = logging.getLogger(__name__)


def _log_context(
    provider_id: int,
    import_run_id: int | None,
    started_at: float,
) -> dict[str, int | float | None]:
    return {
        "provider_id": provider_id,
        "import_run_id": import_run_id,
        "duration_seconds": round(perf_counter() - started_at, 3),
    }


class ImportOrchestrationService:
    """Download, stage, and promote one configured provider feed."""

    def __init__(self, provider_id: int) -> None:
        self._provider_id = provider_id
        self._import_run_id: int | None = None
        self._started_at = perf_counter()

    def run(self, *, retries_exhausted: bool) -> dict[str, object]:
        """Import one provider feed and report the outcome.

        Returns a serializable summary for every terminal outcome. A transient
        failure with retries left is re-raised instead, so the caller's retry
        policy can schedule another attempt.
        """
        session = SessionLocal()

        logger.info("Provider import task started", extra=self._log_context())

        try:
            return self._import(session)
        except (TransientImportError, OperationalError) as error:
            return self._handle_transient_failure(
                error, retries_exhausted=retries_exhausted
            )
        except Exception as error:
            return self._handle_failure(error)
        finally:
            session.close()

    def _import(self, session: Session) -> dict[str, object]:
        provider_repo = PromotionRepository(session)
        provider = provider_repo.get_provider(self._provider_id)
        if provider is None:
            raise ValueError(f"Provider {self._provider_id} does not exist")
        if not provider.is_active:
            raise ValueError(f"Provider {self._provider_id} is inactive")

        self._import_run_id = ImportService.start_run(
            self._provider_id,
            source_uri=provider.feed_url,
        )

        with DownloadService().download(provider) as downloaded_feed:
            import_summary = ImportService(
                downloaded_feed.xml_path,
                provider_id=self._provider_id,
                import_run_id=self._import_run_id,
            ).run()

        promotion_summary = PromotionService(self._import_run_id).run()

        status = self._reconciled_run_status(session, provider_repo)
        result = {
            "status": status,
            "provider_id": self._provider_id,
            "import_run_id": self._import_run_id,
            "records_received": import_summary.total_jobs,
            "records_staged": import_summary.valid_jobs,
            "records_rejected": import_summary.invalid_jobs,
            "new_jobs": promotion_summary.new_jobs,
            "updated_jobs": promotion_summary.updated_jobs,
            "unchanged_jobs": promotion_summary.unchanged_jobs,
            "deactivated_jobs": promotion_summary.deactivated_jobs,
        }

        if status == "completed":
            logger.info("Provider import task completed", extra=self._log_context())
        else:
            logger.error(
                "Provider import task finished with a failed import run",
                extra=self._log_context(),
            )
        return result

    def _reconciled_run_status(
        self,
        session: Session,
        provider_repo: PromotionRepository,
    ) -> str:
        # Refresh through the repository so anomaly-aborted promotions are
        # reported as failures even though PromotionService returns normally.
        session.rollback()
        run = provider_repo.get_import_run(self._import_run_id)
        return run.status if run is not None else "failed"

    def _handle_transient_failure(
        self,
        error: TransientImportError | OperationalError,
        *,
        retries_exhausted: bool,
    ) -> dict[str, object]:
        transient_error = (
            error
            if isinstance(error, TransientImportError)
            else TransientImportError(
                f"Temporary database failure while loading provider: {error}",
                import_run_id=self._import_run_id,
            )
        )
        self._import_run_id = self._import_run_id or transient_error.import_run_id
        if self._import_run_id is not None:
            ImportService.mark_failed(self._import_run_id, transient_error)

        if retries_exhausted:
            logger.exception(
                "Provider import task exhausted retries",
                extra=self._log_context(),
            )
            return self._failure(transient_error)

        logger.warning(
            "Provider import task will retry",
            extra=self._log_context(),
            exc_info=True,
        )
        if transient_error is error:
            raise error
        raise transient_error from error

    def _handle_failure(self, error: Exception) -> dict[str, object]:
        if self._import_run_id is not None:
            ImportService.mark_failed(self._import_run_id, error)
        logger.exception("Provider import task failed", extra=self._log_context())
        return self._failure(error)

    def _failure(self, error: Exception) -> dict[str, object]:
        return {
            "status": "failed",
            "provider_id": self._provider_id,
            "import_run_id": self._import_run_id,
            "error": str(error),
        }

    def _log_context(self) -> dict[str, int | float | None]:
        return _log_context(self._provider_id, self._import_run_id, self._started_at)
