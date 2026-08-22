"""Affiliate-link lookup and generation business logic."""

from __future__ import annotations

import secrets
from typing import Any
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.affiliate_repositories import AffiliateRepository

MAX_SHORT_HASH_ATTEMPTS = 3
SHORT_HASH_CONSTRAINT = "ix_affiliate_links_short_hash"


class AffiliateService:
    """Coordinate affiliate-link lookup, creation, and collision recovery."""

    @staticmethod
    def generate_short_hash() -> str:
        """Generate a cryptographically random URL-safe short identifier."""
        return secrets.token_urlsafe(8)

    async def lookup_jobs(
        self,
        session: AsyncSession,
        provider_id: int,
        source_job_ids: list[str],
    ) -> dict[str, list[Any]]:
        """Group source references into matched jobs and missing references."""
        matched = await AffiliateRepository(session).lookup_jobs_by_references(
            provider_id,
            source_job_ids,
        )
        matched_references = {row["source_job_id"] for row in matched}
        return {
            "matched": matched,
            "not_found": [
                source_job_id
                for source_job_id in source_job_ids
                if source_job_id not in matched_references
            ],
        }

    async def generate_links(
        self,
        session: AsyncSession,
        provider_id: int,
        job_ids: list[int],
        admin_id: UUID | None = None,
    ) -> list[dict[str, Any]]:
        """Create one idempotent affiliate link per job and commit the transaction."""
        unique_job_ids = list(dict.fromkeys(job_ids))
        if not unique_job_ids:
            return []

        repository = AffiliateRepository(session)
        for attempt in range(MAX_SHORT_HASH_ATTEMPTS):
            links = [
                {
                    "job_id": job_id,
                    "provider_id": provider_id,
                    "short_hash": self.generate_short_hash(),
                    "created_by_admin_id": admin_id,
                }
                for job_id in unique_job_ids
            ]
            try:
                rows = await repository.create_affiliate_links(links)
                await session.commit()
                return [
                    {
                        "job_id": row["job_id"],
                        "short_hash": row["short_hash"],
                        "redirect_url": f"/r/{row['short_hash']}",
                    }
                    for row in rows
                ]
            except IntegrityError as error:
                await session.rollback()
                if (
                    not self._is_short_hash_collision(error)
                    or attempt == MAX_SHORT_HASH_ATTEMPTS - 1
                ):
                    raise
            except Exception:
                await session.rollback()
                raise

        raise RuntimeError("Affiliate short-hash retry loop exited unexpectedly")

    @staticmethod
    def _is_short_hash_collision(error: IntegrityError) -> bool:
        """Return whether PostgreSQL reported the short-hash unique index."""
        original = error.orig
        candidates = (original, getattr(original, "__cause__", None))
        return any(
            getattr(candidate, "constraint_name", None) == SHORT_HASH_CONSTRAINT
            for candidate in candidates
            if candidate is not None
        ) or SHORT_HASH_CONSTRAINT in str(original)
