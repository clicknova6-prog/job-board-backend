"""Celery application configuration."""

import os
import pkgutil
from datetime import timedelta
from pathlib import Path

from celery import Celery
from dotenv import load_dotenv

import app.tasks

load_dotenv(Path(__file__).resolve().parents[1] / ".env", encoding="utf-8-sig")

celery_app = Celery(
    "job_board",
    broker=os.environ.get("REDIS_BROKER_URL", "redis://localhost:6379/0"),
    backend=os.environ.get("REDIS_RESULT_BACKEND_URL", "redis://localhost:6379/1"),
)

# Per the locked spec, import_runs is the authoritative source of import
# outcomes; Celery's result backend is disposable and for debugging only.
celery_app.autodiscover_tasks(
    [
        module.name
        for module in pkgutil.iter_modules(
            app.tasks.__path__,
            prefix=f"{app.tasks.__name__}.",
        )
    ],
    related_name=None,
    force=True,
)

celery_app.conf.beat_schedule = {
    "dispatch-provider-imports": {
        "task": "app.tasks.scheduler_tasks.dispatch_provider_imports",
        "schedule": timedelta(minutes=2),
    },
    "hard-delete-expired-jobs": {
        "task": "app.tasks.cleanup_tasks.hard_delete_expired_jobs",
        "schedule": timedelta(minutes=30),
    },
}

# On Windows, run workers with --pool=solo because Celery's default prefork
# pool is not supported: celery -A app.celery_app worker --loglevel=info --pool=solo
