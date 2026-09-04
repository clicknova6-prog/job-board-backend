"""Celery Beat dispatcher for provider feed imports."""

from __future__ import annotations

import logging
from dataclasses import asdict

from app.celery_app import celery_app
from app.db.repositories import SchedulerRepository
from app.db.session import SessionLocal
from app.imports.scheduler import ProviderSchedulerService

logger = logging.getLogger(__name__)

RUN_PROVIDER_IMPORT_TASK_NAME = "app.tasks.import_tasks.run_provider_import"


@celery_app.task(name="app.tasks.scheduler_tasks.dispatch_provider_imports")
def dispatch_provider_imports() -> dict[str, list[int]]:
    """Enqueue active providers whose configured import interval has elapsed."""
    with SessionLocal() as session:
        plan = ProviderSchedulerService(
            SchedulerRepository(session)
        ).build_dispatch_plan()

    for provider_id in plan.due_provider_ids:
        celery_app.send_task(RUN_PROVIDER_IMPORT_TASK_NAME, args=[provider_id])

    result = asdict(plan)
    result["enqueued_provider_ids"] = result.pop("due_provider_ids")
    logger.info("Provider import dispatch completed", extra=result)
    return result
