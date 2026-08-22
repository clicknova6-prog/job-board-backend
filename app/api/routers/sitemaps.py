"""Crawler-facing routes for pre-generated sitemap files."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.core.config import SitemapSettings

router = APIRouter(tags=["Sitemaps"])


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
