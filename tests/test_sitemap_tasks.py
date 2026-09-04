"""Unit tests for the sitemap generation Celery task wiring."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import Mock

import pytest

# app.celery_app is imported first on purpose: its autodiscovery loads every
# task module, so importing a task module first is a circular import.
from app.celery_app import celery_app
from app.services.sitemap_service import SitemapManifest
from app.tasks import sitemap_tasks
from app.tasks.sitemap_tasks import generate_sitemaps_task

GENERATED_AT = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
MANIFEST = SitemapManifest(
    filenames=["sitemap-1.xml.gz", "sitemap-2.xml.gz"],
    total_job_count=75_000,
    generated_at=GENERATED_AT,
)


def test_task_generates_sitemaps_and_returns_a_serializable_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generate = Mock(name="generate_sitemaps", return_value=MANIFEST)
    monkeypatch.setattr(sitemap_tasks, "generate_sitemaps", generate)

    result = generate_sitemaps_task.apply().result

    generate.assert_called_once_with()
    assert result == {
        "filenames": ["sitemap-1.xml.gz", "sitemap-2.xml.gz"],
        "total_job_count": 75_000,
        "generated_at": GENERATED_AT.isoformat(),
    }
    # The Celery result backend only carries JSON, so the timestamp must not
    # leave the task as a datetime.
    assert isinstance(result["generated_at"], str)


def test_sitemap_task_is_registered_under_its_documented_name() -> None:
    assert generate_sitemaps_task.name == "app.tasks.sitemap_tasks.generate_sitemaps"
    assert generate_sitemaps_task.name in celery_app.tasks
