"""Async persistence operations for affiliate-link workflows."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Text, any_, bindparam, select
from sqlalchemy.dialects.postgresql import ARRAY, insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AffiliateLink, Job


class AffiliateRepository:
    """All ORM access required by the affiliate-link service layer."""

    def __init__(self, session: AsyncSession) -> None:
        """Store the async SQLAlchemy session."""
        self._session = session

    async def lookup_jobs_by_references(
        self,
        provider_id: int,
        source_job_ids: list[str],
    ) -> list[dict[str, Any]]:
        """Return matching jobs and their existing affiliate-link state."""
        if not source_job_ids:
            return []

        source_ids = bindparam("source_job_ids", type_=ARRAY(Text))
        statement = (
            select(
                Job.id,
                Job.source_job_id,
                Job.title,
                Job.advertiser_name,
                Job.apply_url,
                Job.is_active,
                AffiliateLink.id.is_not(None).label("has_affiliate_link"),
                AffiliateLink.short_hash,
            )
            .outerjoin(AffiliateLink, AffiliateLink.job_id == Job.id)
            .where(
                Job.provider_id == provider_id,
                Job.source_job_id == any_(source_ids),
            )
            .order_by(Job.id)
        )
        result = await self._session.execute(
            statement,
            {"source_job_ids": source_job_ids},
        )
        return [dict(row) for row in result.mappings()]

    async def lookup_jobs_by_ids(
        self,
        provider_id: int,
        job_ids: list[int],
    ) -> list[dict[str, Any]]:
        """Return confirmed jobs and apply URLs for link generation."""
        if not job_ids:
            return []

        result = await self._session.execute(
            select(Job.id, Job.apply_url).where(
                Job.provider_id == provider_id,
                Job.id.in_(job_ids),
            )
        )
        return [dict(row) for row in result.mappings()]

    async def create_affiliate_links(
        self, links: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Idempotently create links and return all rows for the requested jobs."""
        if not links:
            return []

        values = [
            {
                "job_id": link["job_id"],
                "provider_id": link["provider_id"],
                "short_hash": link["short_hash"],
                "created_by_admin_id": link.get("created_by_admin_id"),
            }
            for link in links
        ]
        await self._session.execute(
            insert(AffiliateLink)
            .values(values)
            .on_conflict_do_nothing(index_elements=[AffiliateLink.job_id])
        )

        job_ids = list(dict.fromkeys(link["job_id"] for link in links))
        result = await self._session.execute(
            select(
                AffiliateLink.id,
                AffiliateLink.short_hash,
                AffiliateLink.job_id,
                AffiliateLink.provider_id,
                AffiliateLink.created_by_admin_id,
                AffiliateLink.created_at,
            )
            .where(AffiliateLink.job_id.in_(job_ids))
            .order_by(AffiliateLink.job_id)
        )
        return [dict(row) for row in result.mappings()]

    async def get_by_short_hash(self, short_hash: str) -> dict[str, Any] | None:
        """Resolve a short hash to its job redirect state."""
        result = await self._session.execute(
            select(
                AffiliateLink.short_hash,
                Job.apply_url,
                Job.is_active,
                Job.slug,
            )
            .join(Job, Job.id == AffiliateLink.job_id)
            .where(AffiliateLink.short_hash == short_hash)
        )
        row = result.mappings().one_or_none()
        return None if row is None else dict(row)
