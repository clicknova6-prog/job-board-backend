"""Shared seeding of the default test site for the public job API tests.

Public job endpoints are scoped by ``X-Site-Key``; these helpers create one
active site and assign every seeded job to it, so the pre-existing tests keep
exercising the same behaviour through a site.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import delete, insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import JobSite, Site

TEST_SITE_KEY = "test-site-key"
TEST_SITE_BASE_URL = "https://it.example.test"
SITE_HEADERS = {"X-Site-Key": TEST_SITE_KEY}


async def seed_test_site(session: AsyncSession, job_ids: Sequence[int]) -> int:
    """Replace all sites with the default test site; assign ``job_ids`` to it."""
    await session.execute(delete(Site))
    site_id = (
        await session.execute(
            insert(Site)
            .values(
                slug="test-site",
                name="Test Site",
                site_key=TEST_SITE_KEY,
                base_url=TEST_SITE_BASE_URL,
            )
            .returning(Site.id)
        )
    ).scalar_one()
    if job_ids:
        await session.execute(
            insert(JobSite),
            [{"job_id": job_id, "site_id": site_id} for job_id in job_ids],
        )
    return site_id
