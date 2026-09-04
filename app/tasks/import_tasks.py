"""Celery orchestration for provider feed imports."""

from __future__ import annotations

from typing import Any

from app.celery_app import celery_app
from app.imports.exceptions import TransientImportError
from app.services.import_orchestration_service import ImportOrchestrationService


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
    return ImportOrchestrationService(provider_id).run(
        retries_exhausted=self.request.retries >= self.max_retries,
    )
