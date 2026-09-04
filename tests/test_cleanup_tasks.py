"""Unit tests for the expired-job cleanup Celery task wiring."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

# app.celery_app is imported first on purpose: its autodiscovery loads every
# task module, so importing a task module first is a circular import.
from app.celery_app import celery_app
from app.imports.cleanup import CleanupSummary, ProviderCleanupSummary
from app.tasks import cleanup_tasks
from app.tasks.cleanup_tasks import hard_delete_expired_jobs

SUMMARY = CleanupSummary(
    checked_jobs=12,
    deleted_jobs=5,
    providers=[
        ProviderCleanupSummary(
            provider_id=7,
            provider_name="jobg8",
            retention_hours=12,
            checked_jobs=12,
            deleted_jobs=5,
        )
    ],
)


def test_task_runs_the_cleanup_service_and_serializes_its_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleanup_service = Mock(name="CleanupService")
    cleanup_service.return_value.run.return_value = SUMMARY
    monkeypatch.setattr(cleanup_tasks, "CleanupService", cleanup_service)

    result = hard_delete_expired_jobs.apply().result

    cleanup_service.assert_called_once_with()
    cleanup_service.return_value.run.assert_called_once_with()
    assert result == {
        "checked_jobs": 12,
        "deleted_jobs": 5,
        "providers": [
            {
                "provider_id": 7,
                "provider_name": "jobg8",
                "retention_hours": 12,
                "checked_jobs": 12,
                "deleted_jobs": 5,
            }
        ],
    }


def test_cleanup_task_is_registered_under_its_documented_name() -> None:
    assert (
        hard_delete_expired_jobs.name
        == "app.tasks.cleanup_tasks.hard_delete_expired_jobs"
    )
    assert hard_delete_expired_jobs.name in celery_app.tasks
