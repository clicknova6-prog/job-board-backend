"""Celery orchestration for sitemap generation."""

from __future__ import annotations

import logging
from dataclasses import asdict

from app.celery_app import celery_app
from app.services.sitemap_service import generate_sitemaps

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.sitemap_tasks.generate_sitemaps")
def generate_sitemaps_task() -> dict[str, object]:
    """Generate sitemap files and return a serializable manifest."""
    result = asdict(generate_sitemaps())
    result["generated_at"] = result["generated_at"].isoformat()
    logger.info("Sitemap task completed", extra=result)
    return result
