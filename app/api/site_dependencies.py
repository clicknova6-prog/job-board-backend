"""FastAPI dependency resolving the calling niche site from ``X-Site-Key``."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import CORS_ALLOWED_ORIGINS
from app.db.async_session import get_async_session
from app.db.site_repositories import PublicSiteRepository, SiteRecord, origin_of

SITE_KEY_HEADER = "X-Site-Key"

# Hub-wide origins (env CORS_ALLOWED_ORIGINS, e.g. the admin panel or local
# dev) may call on behalf of any site.
_GLOBAL_ORIGINS = frozenset(
    origin for origin in map(origin_of, CORS_ALLOWED_ORIGINS) if origin is not None
)


async def get_current_site(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_async_session)],
    site_key: Annotated[
        str | None,
        Header(
            alias=SITE_KEY_HEADER,
            description="Public key identifying the niche job board site.",
        ),
    ] = None,
) -> SiteRecord:
    """Return the active site for this request or reject it.

    - missing header -> 400
    - unknown or deactivated site -> 404
    - browser ``Origin`` not allowed for this site -> 403 (server-side calls
      without an ``Origin`` header are not restricted; the key is not a secret,
      this only stops one site's frontend from reading another site's data)
    """
    if not site_key or not site_key.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{SITE_KEY_HEADER} header is required",
        )

    site = await PublicSiteRepository(session).get_active_by_key(site_key.strip())
    if site is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Unknown site"
        )

    raw_origin = request.headers.get("origin")
    if raw_origin is not None:
        origin = origin_of(raw_origin)
        if origin is None or (
            origin not in _GLOBAL_ORIGINS and origin not in site.allowed_origins
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Origin is not allowed for this site",
            )

    return site


CurrentSite = Annotated[SiteRecord, Depends(get_current_site)]
