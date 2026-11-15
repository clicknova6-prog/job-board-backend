"""Unit tests for the site re-routing Celery task wiring."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

# app.celery_app first: its autodiscovery loads every task module.
from app.celery_app import celery_app
from app.services.site_routing_service import SiteRoutingSummary
from app.tasks import site_routing_tasks
from app.tasks.site_routing_tasks import route_all_sites_task


def test_task_routes_all_providers_bumps_filters_and_returns_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = Mock()
    service.route_all_providers.return_value = [
        SiteRoutingSummary(1, 10, 3, 1, 2, {5: 4})
    ]
    monkeypatch.setattr(site_routing_tasks, "SiteRoutingService", lambda: service)
    bump = Mock()
    monkeypatch.setattr(
        site_routing_tasks.filters_cache_invalidator, "bump_version", bump
    )

    result = route_all_sites_task.apply().result

    bump.assert_called_once_with()
    assert result == {
        "providers": [
            {
                "provider_id": 1,
                "jobs_evaluated": 10,
                "assigned": 3,
                "reassigned": 1,
                "removed": 2,
                "jobs_per_site": {"5": 4},
            }
        ]
    }
    assert route_all_sites_task.name in celery_app.tasks
