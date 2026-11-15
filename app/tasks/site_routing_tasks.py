"""Celery task: re-route every provider's jobs to sites (e.g. after rule edits)."""

from __future__ import annotations

import logging
from dataclasses import asdict

from app.celery_app import celery_app
from app.core.filters_cache import filters_cache_invalidator
from app.services.site_routing_service import SiteRoutingService

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.site_routing_tasks.route_all_sites")
def route_all_sites_task() -> dict[str, object]:
    """Recompute ``job_sites`` for all providers and refresh cached filters."""
    summaries = SiteRoutingService().route_all_providers()
    filters_cache_invalidator.bump_version()
    result: dict[str, object] = {
        "providers": [
            {
                **asdict(summary),
                "jobs_per_site": {
                    str(site_id): count
                    for site_id, count in summary.jobs_per_site.items()
                },
            }
            for summary in summaries
        ]
    }
    logger.info("Site re-routing task completed", extra=result)
    return result
