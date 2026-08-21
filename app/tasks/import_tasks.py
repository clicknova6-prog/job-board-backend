"""Celery orchestration for provider feed imports."""

from __future__ import annotations

import logging
from time import perf_counter
from typing import Any

from sqlalchemy.exc import OperationalError

from app.celery_app import celery_app
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


@celery_app.task(
    bind=True,
    name="app.tasks.import_tasks.run_provider_import",
    autoretry_for=(TransientImportError,),
    max_retries=3,
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
)
def run_provider_import(self: Any, provider_id: int) -> dict[str, object]:
    """Download, stage, and promote one configured provider feed."""
    started_at = perf_counter()
    import_run_id: int | None = None
    session = SessionLocal()

    logger.info(
        "Provider import task started",
        extra=_log_context(provider_id, import_run_id, started_at),
    )

    try:
        provider_repo = PromotionRepository(session)
        provider = provider_repo.get_provider(provider_id)
        if provider is None:
            raise ValueError(f"Provider {provider_id} does not exist")
        if not provider.is_active:
            raise ValueError(f"Provider {provider_id} is inactive")

        import_run_id = ImportService.start_run(
            provider_id,
            source_uri=provider.feed_url,
        )

        with DownloadService().download(provider) as downloaded_feed:
            import_summary = ImportService(
                downloaded_feed.xml_path,
                provider_id=provider_id,
                import_run_id=import_run_id,
            ).run()

        promotion_summary = PromotionService(import_run_id).run()

        # Refresh through the repository so anomaly-aborted promotions are
        # reported as failures even though PromotionService returns normally.
        session.rollback()
        run = provider_repo.get_import_run(import_run_id)
        status = run.status if run is not None else "failed"
        result = {
            "status": status,
            "provider_id": provider_id,
            "import_run_id": import_run_id,
            "records_received": import_summary.total_jobs,
            "records_staged": import_summary.valid_jobs,
            "records_rejected": import_summary.invalid_jobs,
            "new_jobs": promotion_summary.new_jobs,
            "updated_jobs": promotion_summary.updated_jobs,
            "unchanged_jobs": promotion_summary.unchanged_jobs,
            "deactivated_jobs": promotion_summary.deactivated_jobs,
        }

        if status == "completed":
            logger.info(
                "Provider import task completed",
                extra=_log_context(provider_id, import_run_id, started_at),
            )
        else:
            logger.error(
                "Provider import task finished with a failed import run",
                extra=_log_context(provider_id, import_run_id, started_at),
            )
        return result

    except (TransientImportError, OperationalError) as error:
        transient_error = (
            error
            if isinstance(error, TransientImportError)
            else TransientImportError(
                f"Temporary database failure while loading provider: {error}",
                import_run_id=import_run_id,
            )
        )
        import_run_id = import_run_id or transient_error.import_run_id
        if import_run_id is not None:
            ImportService.mark_failed(import_run_id, transient_error)

        if self.request.retries >= self.max_retries:
            logger.exception(
                "Provider import task exhausted retries",
                extra=_log_context(provider_id, import_run_id, started_at),
            )
            return {
                "status": "failed",
                "provider_id": provider_id,
                "import_run_id": import_run_id,
                "error": str(transient_error),
            }

        logger.warning(
            "Provider import task will retry",
            extra=_log_context(provider_id, import_run_id, started_at),
            exc_info=True,
        )
        if transient_error is error:
            raise
        raise transient_error from error

    except Exception as error:
        if import_run_id is not None:
            ImportService.mark_failed(import_run_id, error)
        logger.exception(
            "Provider import task failed",
            extra=_log_context(provider_id, import_run_id, started_at),
        )
        return {
            "status": "failed",
            "provider_id": provider_id,
            "import_run_id": import_run_id,
            "error": str(error),
        }
    finally:
        session.close()
