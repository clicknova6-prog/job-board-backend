"""Maintain ``job_sites``: which niche sites each hub job belongs to.

Routing runs as its own step after a completed promotion (and on demand when
rules change). It recomputes the full desired assignment set for one provider
and applies only the difference, so it is idempotent and automatically
picks up rule edits on the next run. A routing failure never affects the
import outcome: previous assignments simply stay in place.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field

from app.db.repositories import SiteRoutingRepository
from app.db.session import SessionLocal
from app.sites.matcher import RoutingRule, SiteMatcher

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SiteRoutingSummary:
    """What one routing pass changed for one provider."""

    provider_id: int
    jobs_evaluated: int
    assigned: int
    reassigned: int
    removed: int
    jobs_per_site: dict[int, int] = field(default_factory=dict)


class SiteRoutingService:
    """Recompute and apply site assignments for providers."""

    def __init__(self, *, session_factory=SessionLocal) -> None:
        self._session_factory = session_factory

    def route_provider(self, provider_id: int) -> SiteRoutingSummary:
        """Bring ``job_sites`` for one provider in line with the active rules."""
        with self._session_factory() as session:
            repo = SiteRoutingRepository(session)
            try:
                summary = self._route(repo, provider_id)
                repo.commit()
            except Exception:
                repo.rollback()
                raise
        logger.info(
            "Site routing completed: provider_id=%d evaluated=%d assigned=%d "
            "reassigned=%d removed=%d per_site=%s",
            summary.provider_id,
            summary.jobs_evaluated,
            summary.assigned,
            summary.reassigned,
            summary.removed,
            summary.jobs_per_site,
        )
        return summary

    def route_all_providers(self) -> list[SiteRoutingSummary]:
        """Re-route every provider, e.g. after an admin edits rules."""
        with self._session_factory() as session:
            provider_ids = SiteRoutingRepository(session).list_provider_ids()
        return [self.route_provider(provider_id) for provider_id in provider_ids]

    @staticmethod
    def _route(repo: SiteRoutingRepository, provider_id: int) -> SiteRoutingSummary:
        site_ids = repo.list_active_site_ids()
        matcher = SiteMatcher(
            (
                RoutingRule(
                    id=row.id,
                    site_id=row.site_id,
                    rule_type=row.rule_type,
                    pattern=row.pattern,
                    provider_id=row.provider_id,
                )
                for row in repo.list_active_rules()
            ),
            provider_id=provider_id,
        )

        desired: dict[tuple[int, int], int] = {}
        jobs = repo.load_provider_jobs(provider_id)
        for job in jobs:
            for site_id, rule_id in matcher.match(
                title=job.title, classification=job.classification
            ).items():
                desired[(job.id, site_id)] = rule_id

        # Only active sites are managed; a deactivated site keeps its rows
        # untouched until it is reactivated or deleted.
        existing = repo.load_assignments(provider_id, site_ids)

        to_insert = [key for key in desired if key not in existing]
        to_repoint = [
            key
            for key, rule_id in desired.items()
            if key in existing and existing[key] != rule_id
        ]
        to_delete = [key for key in existing if key not in desired]

        repo.upsert_assignments(
            [
                (job_id, site_id, desired[(job_id, site_id)])
                for job_id, site_id in to_insert + to_repoint
            ]
        )
        repo.delete_assignments(to_delete)

        return SiteRoutingSummary(
            provider_id=provider_id,
            jobs_evaluated=len(jobs),
            assigned=len(to_insert),
            reassigned=len(to_repoint),
            removed=len(to_delete),
            jobs_per_site=dict(Counter(site_id for _, site_id in desired)),
        )
