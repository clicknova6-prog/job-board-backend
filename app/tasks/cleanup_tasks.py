"""Celery orchestration for expired job cleanup."""

from __future__ import annotations

from dataclasses import asdict

from app.celery_app import celery_app
from app.imports.cleanup import CleanupService


@celery_app.task(name="app.tasks.cleanup_tasks.hard_delete_expired_jobs")
def hard_delete_expired_jobs() -> dict[str, object]:
    """Run the expired soft-deleted job cleanup service."""
    return asdict(CleanupService().run())
