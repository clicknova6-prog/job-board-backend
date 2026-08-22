"""Crawler-facing routes for pre-generated sitemap files."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse

from app.core.config import SitemapSettings

router = APIRouter(tags=["Sitemaps"])


@router.get("/robots.txt", response_class=PlainTextResponse)
def robots_txt() -> PlainTextResponse:
    """Serve crawler directives using the configured public site URL."""
    base_url = SitemapSettings.from_environment().public_site_base_url
    content = (
        "User-agent: *\n"
        "Allow: /job/\n"
        "Disallow: /*?\n"
        "Disallow: /admin\n"
        "Disallow: /r/\n"
        "\n"
        f"Sitemap: {base_url}/sitemap.xml\n"
    )
    return PlainTextResponse(content)


def _sitemap_file(filename: str):
    path = SitemapSettings.from_environment().output_dir.resolve() / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Sitemap not found")
    return path


@router.get("/sitemap.xml", response_class=FileResponse)
def sitemap_index() -> FileResponse:
    """Serve the pre-generated sitemap index."""
    return FileResponse(_sitemap_file("sitemap.xml"), media_type="application/xml")


@router.get("/sitemap-{chunk_number}.xml.gz", response_class=FileResponse)
def sitemap_chunk(chunk_number: str) -> FileResponse:
    """Serve one pre-generated compressed sitemap chunk."""
    if not chunk_number.isdigit() or int(chunk_number) < 1:
        raise HTTPException(status_code=404, detail="Sitemap not found")
    return FileResponse(
        _sitemap_file(f"sitemap-{int(chunk_number)}.xml.gz"),
        media_type="application/gzip",
    )
