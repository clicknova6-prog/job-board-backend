"""Async persistence for site and routing-rule administration.

Mutations flush but never commit, so routers can audit and commit atomically
(same contract as ``ProviderRepository``). Unique/foreign-key violations are
translated into ``SiteAdminConflictError``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Job, JobSite, Provider, Site, SiteRoutingRule


class SiteAdminConflictError(Exception):
    """A mutation violated a uniqueness or reference constraint."""


_CONSTRAINT_MESSAGES = {
    "sites_slug_key": "A site with this slug already exists",
    "sites_site_key_key": "A site with this site_key already exists",
    "uq_site_routing_rules_site_provider_type_pattern": (
        "An identical rule already exists for this site"
    ),
}


@dataclass(frozen=True, slots=True)
class SiteAdminRecord:
    id: int
    slug: str
    name: str
    site_key: str
    base_url: str
    is_active: bool
    config: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    active_job_count: int = 0
    rule_count: int = 0


@dataclass(frozen=True, slots=True)
class RoutingRuleRecord:
    id: int
    site_id: int
    provider_id: int | None
    rule_type: str
    pattern: str
    is_active: bool
    notes: str | None
    created_at: datetime


def _site_record(
    site: Site, *, active_jobs: int = 0, rules: int = 0
) -> SiteAdminRecord:
    return SiteAdminRecord(
        id=site.id,
        slug=site.slug,
        name=site.name,
        site_key=site.site_key,
        base_url=site.base_url,
        is_active=site.is_active,
        config=dict(site.config or {}),
        created_at=site.created_at,
        updated_at=site.updated_at,
        active_job_count=active_jobs,
        rule_count=rules,
    )


def _rule_record(rule: SiteRoutingRule) -> RoutingRuleRecord:
    return RoutingRuleRecord(
        id=rule.id,
        site_id=rule.site_id,
        provider_id=rule.provider_id,
        rule_type=rule.rule_type,
        pattern=rule.pattern,
        is_active=rule.is_active,
        notes=rule.notes,
        created_at=rule.created_at,
    )


def _constraint_name(exc: IntegrityError) -> str | None:
    """Find the violated constraint name across DB-API driver wrappers."""
    error: BaseException | None = exc.orig
    while error is not None:
        name = getattr(error, "constraint_name", None) or getattr(
            getattr(error, "diag", None), "constraint_name", None
        )
        if name:
            return name
        error = error.__cause__
    return None


class SiteAdminRepository:
    """All ORM access required by the admin sites routes."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ---- sites -----------------------------------------------------------

    async def list_sites(self) -> list[SiteAdminRecord]:
        rows = await self._session.execute(self._site_query().order_by(Site.id))
        return [
            _site_record(site, active_jobs=jobs, rules=rule_count)
            for site, jobs, rule_count in rows
        ]

    async def get_site(self, site_id: int) -> SiteAdminRecord | None:
        row = (
            await self._session.execute(self._site_query().where(Site.id == site_id))
        ).one_or_none()
        if row is None:
            return None
        site, jobs, rule_count = row
        return _site_record(site, active_jobs=jobs, rules=rule_count)

    @staticmethod
    def _site_query():
        active_jobs = (
            select(func.count())
            .select_from(JobSite)
            .join(Job, Job.id == JobSite.job_id)
            .where(JobSite.site_id == Site.id, Job.is_active.is_(True))
            .scalar_subquery()
        )
        rules = (
            select(func.count())
            .select_from(SiteRoutingRule)
            .where(SiteRoutingRule.site_id == Site.id)
            .scalar_subquery()
        )
        return select(Site, active_jobs, rules)

    async def site_exists(self, site_id: int) -> bool:
        return await self._session.get(Site, site_id) is not None

    async def provider_exists(self, provider_id: int) -> bool:
        return await self._session.get(Provider, provider_id) is not None

    async def create_site(self, **fields: Any) -> SiteAdminRecord:
        site = Site(**fields)
        self._session.add(site)
        await self._flush()
        await self._session.refresh(site)
        return _site_record(site)

    async def update_site(
        self, site_id: int, **fields: Any
    ) -> tuple[SiteAdminRecord, SiteAdminRecord] | None:
        site = await self._session.scalar(
            select(Site).where(Site.id == site_id).with_for_update()
        )
        if site is None:
            return None
        before = _site_record(site)
        for name, value in fields.items():
            setattr(site, name, value)
        site.updated_at = datetime.now(UTC)
        await self._flush()
        return before, _site_record(site)

    # ---- rules -----------------------------------------------------------

    async def list_rules(self, site_id: int) -> list[RoutingRuleRecord]:
        rules = await self._session.scalars(
            select(SiteRoutingRule)
            .where(SiteRoutingRule.site_id == site_id)
            .order_by(SiteRoutingRule.id)
        )
        return [_rule_record(rule) for rule in rules]

    async def create_rule(self, site_id: int, **fields: Any) -> RoutingRuleRecord:
        rule = SiteRoutingRule(site_id=site_id, **fields)
        self._session.add(rule)
        await self._flush()
        await self._session.refresh(rule)
        return _rule_record(rule)

    async def update_rule(
        self, site_id: int, rule_id: int, **fields: Any
    ) -> tuple[RoutingRuleRecord, RoutingRuleRecord] | None:
        rule = await self._get_rule_for_update(site_id, rule_id)
        if rule is None:
            return None
        before = _rule_record(rule)
        for name, value in fields.items():
            setattr(rule, name, value)
        await self._flush()
        return before, _rule_record(rule)

    async def delete_rule(self, site_id: int, rule_id: int) -> RoutingRuleRecord | None:
        rule = await self._get_rule_for_update(site_id, rule_id)
        if rule is None:
            return None
        before = _rule_record(rule)
        await self._session.delete(rule)
        await self._flush()
        return before

    async def _get_rule_for_update(
        self, site_id: int, rule_id: int
    ) -> SiteRoutingRule | None:
        return await self._session.scalar(
            select(SiteRoutingRule)
            .where(SiteRoutingRule.id == rule_id, SiteRoutingRule.site_id == site_id)
            .with_for_update()
        )

    async def _flush(self) -> None:
        try:
            await self._session.flush()
        except IntegrityError as exc:
            await self._session.rollback()
            constraint = _constraint_name(exc)
            raise SiteAdminConflictError(
                _CONSTRAINT_MESSAGES.get(constraint or "", "Conflicting site data")
            ) from exc
