"""Root-level public affiliate redirect route."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import SitemapSettings
from app.db.affiliate_repositories import AffiliateRepository
from app.db.async_session import get_async_session

router = APIRouter(tags=["Affiliate Redirect"])


@router.get("/r/{short_hash}", response_model=None)
async def redirect_affiliate_link(
    short_hash: str,
    session: Annotated[AsyncSession, Depends(get_async_session)],
) -> RedirectResponse:
    """Redirect valid hashes even while their jobs are soft-deleted."""
    link = await AffiliateRepository(session).get_by_short_hash(short_hash)
    if link is None:
        base_url = SitemapSettings.from_environment().public_site_base_url
        return RedirectResponse(url=f"{base_url}/job-unavailable", status_code=302)
    return RedirectResponse(url=link["apply_url"], status_code=302)
