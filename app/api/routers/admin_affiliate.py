"""JWT-protected affiliate-link administration routes."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_admin_role
from app.core.rate_limit import limiter, rate_limit_settings
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

router = APIRouter(
    prefix="/admin/api/affiliate",
    tags=["Admin Affiliate"],
    dependencies=[Depends(require_admin_role(AdminRole.ADMIN, AdminRole.SUPER_ADMIN))],
)


@router.post("/lookup", response_model=AffiliateLookupResponse)
@limiter.limit(rate_limit_settings.admin_api)
async def lookup_affiliate_jobs(
    request: Request,
    payload: AffiliateLookupRequest,
    session: Annotated[AsyncSession, Depends(get_async_session)],
) -> AffiliateLookupResponse:
    """Resolve provider references for administrator review."""
    result = await AffiliateService().lookup_jobs(
        session,
        payload.provider_id,
        payload.source_job_ids,
    )
    return AffiliateLookupResponse(
        matched=[_lookup_match(row) for row in result["matched"]],
        not_found=result["not_found"],
    )


@router.post("/generate", response_model=AffiliateGenerateResponse)
@limiter.limit(rate_limit_settings.admin_api)
async def generate_affiliate_links(
    request: Request,
    payload: AffiliateGenerateRequest,
    session: Annotated[AsyncSession, Depends(get_async_session)],
) -> AffiliateGenerateResponse:
    """Revalidate confirmed jobs and generate their affiliate links."""
    result = await AffiliateService().revalidate_and_generate(
        session,
        payload.provider_id,
        payload.job_ids,
    )
    return AffiliateGenerateResponse(
        generated=[AffiliateGeneratedLink(**item) for item in result["generated"]],
        excluded=[AffiliateExcludedJob(**item) for item in result["excluded"]],
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
