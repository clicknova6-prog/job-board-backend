"""Crawler-facing SEO routes, one set per site.

Crawlers request ``https://<site>/robots.txt`` and ``/sitemap.xml`` from the
site's own domain and send no ``X-Site-Key``, so the site is addressed by its
slug in the path. Each frontend rewrites/proxies:

    /robots.txt            -> /sites/<slug>/robots.txt
    /sitemap.xml           -> /sites/<slug>/sitemap.xml
    /sitemap-<n>.xml.gz    -> /sites/<slug>/sitemap-<n>.xml.gz
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path
from fastapi.responses import FileResponse, PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import SitemapSettings
from app.core.rate_limit import limiter
from app.db.async_session import get_async_session
from app.db.site_repositories import PublicSiteRepository, SiteRecord

router = APIRouter(prefix="/sites/{site_slug}", tags=["Sitemaps"])

SLUG_PATTERN = r"^[a-z0-9]+(-[a-z0-9]+)*$"


async def get_site_by_slug(
    site_slug: Annotated[str, Path(pattern=SLUG_PATTERN)],
    session: Annotated[AsyncSession, Depends(get_async_session)],
) -> SiteRecord:
    """Return the active site named in the path, or 404."""
    site = await PublicSiteRepository(session).get_active_by_slug(site_slug)
    if site is None:
        raise HTTPException(status_code=404, detail="Unknown site")
    return site


SiteBySlug = Annotated[SiteRecord, Depends(get_site_by_slug)]


@router.get("/robots.txt", response_class=PlainTextResponse)
@limiter.exempt
async def robots_txt(site: SiteBySlug) -> PlainTextResponse:
    """Serve crawler directives pointing at the site's own sitemap."""
    content = (
        "User-agent: *\n"
        "Allow: /job/\n"
        "Disallow: /*?\n"
        "Disallow: /admin\n"
        "\n"
        f"Sitemap: {site.canonical_base_url}/sitemap.xml\n"
    )
    return PlainTextResponse(content)


def _sitemap_file(site: SiteRecord, filename: str):
    # site.slug comes from the DB and matches SLUG_PATTERN (DB check
    # constraint), so it cannot traverse out of the output directory.
    path = (
        SitemapSettings.from_environment().output_dir.resolve() / site.slug / filename
    )
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Sitemap not found")
    return path


@router.get("/sitemap.xml", response_class=FileResponse)
@limiter.exempt
async def sitemap_index(site: SiteBySlug) -> FileResponse:
    """Serve the site's pre-generated sitemap index."""
    return FileResponse(
        _sitemap_file(site, "sitemap.xml"), media_type="application/xml"
    )


@router.get("/sitemap-{chunk_number}.xml.gz", response_class=FileResponse)
@limiter.exempt
async def sitemap_chunk(site: SiteBySlug, chunk_number: str) -> FileResponse:
    """Serve one of the site's pre-generated compressed sitemap chunks."""
    if not chunk_number.isdigit() or int(chunk_number) < 1:
        raise HTTPException(status_code=404, detail="Sitemap not found")
    return FileResponse(
        _sitemap_file(site, f"sitemap-{int(chunk_number)}.xml.gz"),
        media_type="application/gzip",
    )
