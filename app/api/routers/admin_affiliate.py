"""JWT-protected affiliate-link administration routes."""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_admin_role
from app.db.affiliate_repositories import AffiliateRepository
from app.db.async_session import get_async_session
from app.db.models import AdminRole
from app.schemas.affiliate import (
    AffiliateExcludedJob,
    AffiliateGeneratedLink,
    AffiliateGenerateRequest,
    AffiliateGenerateResponse,
    AffiliateLookupMatch,
    AffiliateLookupRequest,
    AffiliateLookupResponse,
)
from app.services.affiliate_service import AffiliateService

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/admin/api/affiliate",
    tags=["Admin Affiliate"],
    dependencies=[Depends(require_admin_role(AdminRole.ADMIN, AdminRole.SUPER_ADMIN))],
)


@router.post("/lookup", response_model=AffiliateLookupResponse)
async def lookup_affiliate_jobs(
    request: AffiliateLookupRequest,
    session: Annotated[AsyncSession, Depends(get_async_session)],
) -> AffiliateLookupResponse:
    """Resolve provider references for administrator review."""
    result = await AffiliateService().lookup_jobs(
        session,
        request.provider_id,
        request.source_job_ids,
    )
    return AffiliateLookupResponse(
        matched=[_lookup_match(row) for row in result["matched"]],
        not_found=result["not_found"],
    )


@router.post("/generate", response_model=AffiliateGenerateResponse)
async def generate_affiliate_links(
    request: AffiliateGenerateRequest,
    session: Annotated[AsyncSession, Depends(get_async_session)],
) -> AffiliateGenerateResponse:
    """Revalidate confirmed jobs and generate their affiliate links."""
    requested_job_ids = list(dict.fromkeys(request.job_ids))
    rows = await AffiliateRepository(session).lookup_jobs_by_ids(
        request.provider_id,
        requested_job_ids,
    )
    jobs_by_id = {row["id"]: row for row in rows}

    valid_job_ids: list[int] = []
    excluded: list[AffiliateExcludedJob] = []
    for job_id in requested_job_ids:
        job = jobs_by_id.get(job_id)
        if job is None:
            excluded.append(
                AffiliateExcludedJob(
                    job_id=job_id,
                    reason="Job not found for provider",
                )
            )
        elif job["apply_url"] is None:
            excluded.append(
                AffiliateExcludedJob(
                    job_id=job_id,
                    reason="Apply URL is unavailable",
                )
            )
        else:
            valid_job_ids.append(job_id)

    if excluded:
        logger.warning(
            "Excluded jobs from affiliate-link generation after revalidation",
            extra={"excluded_job_ids": [item.job_id for item in excluded]},
        )

    generated = await AffiliateService().generate_links(
        session,
        request.provider_id,
        valid_job_ids,
    )
    return AffiliateGenerateResponse(
        generated=[AffiliateGeneratedLink(**item) for item in generated],
        excluded=excluded,
    )


def _lookup_match(row: dict[str, Any]) -> AffiliateLookupMatch:
    """Translate a repository projection into the public admin contract."""
    internal_job_id = row["id"]
    return AffiliateLookupMatch(
        job_id=internal_job_id,
        source_job_id=row["source_job_id"],
        title=row["title"],
        advertiser_name=row["advertiser_name"],
        internal_job_id=internal_job_id,
        apply_url_available=row["apply_url"] is not None,
        has_affiliate_link=row["has_affiliate_link"],
        existing_short_hash=row["short_hash"],
    )
