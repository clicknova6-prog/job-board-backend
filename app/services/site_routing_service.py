"""Maintain ``job_sites``: which niche sites each hub job belongs to.

Routing runs as its own step after a completed promotion (and on demand when
rules change). It recomputes the full desired assignment set for one provider
and applies only the difference, so it is idempotent and automatically
picks up rule edits on the next run. A routing failure never affects the
import outcome: previous assignments simply stay in place.
"""

from __future__ import annotations

import logging
import random
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.db.repositories import SiteRoutingRepository
from app.db.session import SessionLocal
from app.sites.matcher import EXCLUDE_TITLE, RoutingRule, SiteMatcher, normalize

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


@dataclass(frozen=True, slots=True)
class DraftRule:
    """A rule to evaluate in a dry run; ``rule_id`` is None for unsaved drafts."""

    rule_type: str
    pattern: str
    provider_id: int | None = None
    rule_id: int | None = None


@dataclass(frozen=True, slots=True)
class RulePreview:
    rule_id: int | None
    position: int
    rule_type: str
    pattern: str
    matched: int
    excluded: int
    sample_titles: list[str]


@dataclass(frozen=True, slots=True)
class SiteRoutingPreview:
    """What a rule set would do to one site's assignments. Nothing is written."""

    site_id: int
    jobs_evaluated: int
    matched: int
    active_matched: int
    currently_assigned: int
    would_add: int
    would_remove: int
    added_samples: list[str]
    removed_samples: list[str]
    rules: list[RulePreview]


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

    def preview_site(
        self,
        site_id: int,
        *,
        draft_rules: Sequence[DraftRule] | None = None,
        sample_size: int = 10,
    ) -> SiteRoutingPreview:
        """Dry-run one site's rules (saved, or ``draft_rules``) over all jobs.

        Rule precedence mirrors real routing: excludes first, then the first
        matching rule in list order (saved rules: id order).
        """
        with self._session_factory() as session:
            repo = SiteRoutingRepository(session)
            if draft_rules is None:
                draft_rules = [
                    DraftRule(row.rule_type, row.pattern, row.provider_id, row.id)
                    for row in repo.list_site_rules(site_id)
                ]
            jobs = repo.load_preview_jobs()
            current = repo.load_site_job_ids(site_id)

        # Matcher rule ids are list positions, so precedence follows the list.
        rules = [
            RoutingRule(position, site_id, d.rule_type, d.pattern, d.provider_id)
            for position, d in enumerate(draft_rules)
        ]
        include_rules = [r for r in rules if r.rule_type != EXCLUDE_TITLE]
        exclude_rules = [r for r in rules if r.rule_type == EXCLUDE_TITLE]
        matchers: dict[int, tuple[SiteMatcher, SiteMatcher]] = {}

        hits: dict[int, list[str]] = defaultdict(list)
        excluded: Counter[int] = Counter()
        desired: set[int] = set()
        active_matched = 0
        titles: dict[int, str] = {}
        for job in jobs:
            if job.provider_id not in matchers:
                matchers[job.provider_id] = (
                    SiteMatcher(rules, provider_id=job.provider_id),
                    SiteMatcher(include_rules, provider_id=job.provider_id),
                )
            full, includes_only = matchers[job.provider_id]
            match = full.match(title=job.title, classification=job.classification)
            titles[job.id] = job.title or ""
            if site_id in match:
                desired.add(job.id)
                active_matched += job.is_active
                hits[match[site_id]].append(job.title or "")
            elif site_id in includes_only.match(
                title=job.title, classification=job.classification
            ):
                haystack = normalize(job.title)
                for rule in exclude_rules:
                    if (
                        rule.provider_id in (None, job.provider_id)
                        and normalize(rule.pattern) in haystack
                    ):
                        excluded[rule.id] += 1
                        break

        rng = random.Random(1)

        def sample(values: Sequence[str]) -> list[str]:
            return rng.sample(list(values), min(sample_size, len(values)))

        added = desired - current
        removed = current - desired
        return SiteRoutingPreview(
            site_id=site_id,
            jobs_evaluated=len(jobs),
            matched=len(desired),
            active_matched=active_matched,
            currently_assigned=len(current),
            would_add=len(added),
            would_remove=len(removed),
            added_samples=sample([titles[i] for i in sorted(added)]),
            removed_samples=sample([titles.get(i, "") for i in sorted(removed)]),
            rules=[
                RulePreview(
                    rule_id=draft.rule_id,
                    position=position,
                    rule_type=draft.rule_type,
                    pattern=draft.pattern,
                    matched=len(hits.get(position, [])),
                    excluded=excluded.get(position, 0),
                    sample_titles=sample(hits.get(position, [])),
                )
                for position, draft in enumerate(draft_rules)
            ],
        )

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
