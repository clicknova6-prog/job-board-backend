"""Smoke tests for Celery application configuration and task discovery."""

from __future__ import annotations

import os
from datetime import timedelta

from app.celery_app import celery_app
from app.core.config import SitemapSettings
from app.tasks.health import ping

EXPECTED_TASK_NAMES = frozenset(
    {
        "app.tasks.cleanup_tasks.hard_delete_expired_jobs",
        "app.tasks.health.ping",
        "app.tasks.import_tasks.run_provider_import",
        "app.tasks.scheduler_tasks.dispatch_provider_imports",
        "app.tasks.sitemap_tasks.generate_sitemaps",
    }
)


def test_broker_and_result_backend_come_from_the_environment() -> None:
    assert celery_app.conf.broker_url == os.environ.get(
        "REDIS_BROKER_URL", "redis://localhost:6379/0"
    )
    assert celery_app.conf.result_backend == os.environ.get(
        "REDIS_RESULT_BACKEND_URL", "redis://localhost:6379/1"
    )
    # The broker and the disposable result backend must not share a Redis DB.
    assert celery_app.conf.broker_url != celery_app.conf.result_backend


def test_autodiscovery_registers_every_task_module() -> None:
    assert EXPECTED_TASK_NAMES <= set(celery_app.tasks)


def test_beat_schedule_targets_registered_tasks_on_expected_intervals() -> None:
    schedule = celery_app.conf.beat_schedule

    assert set(schedule) == {
        "dispatch-provider-imports",
        "hard-delete-expired-jobs",
        "generate-sitemaps",
    }
    for entry in schedule.values():
        assert entry["task"] in celery_app.tasks

    assert schedule["dispatch-provider-imports"]["schedule"] == timedelta(minutes=2)
    assert schedule["hard-delete-expired-jobs"]["schedule"] == timedelta(minutes=30)
    assert schedule["generate-sitemaps"]["schedule"] == timedelta(
        minutes=SitemapSettings.from_environment().regen_interval_minutes
    )


def test_health_task_returns_a_static_response() -> None:
    assert ping() == "pong"
    assert celery_app.tasks["app.tasks.health.ping"].run() == "pong"
