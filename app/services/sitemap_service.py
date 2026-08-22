"""Generate crawler-facing sitemap files from active jobs."""

from __future__ import annotations

import gzip
import logging
import os
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4
from xml.etree.ElementTree import Element, SubElement, tostring

from app.core.config import SitemapSettings
from app.db.repositories import SitemapJob, SitemapRepository
from app.db.session import SessionLocal

logger = logging.getLogger(__name__)
SITEMAP_NAMESPACE = "http://www.sitemaps.org/schemas/sitemap/0.9"


@dataclass(frozen=True, slots=True)
class SitemapManifest:
    """Small generation result suitable for task logging."""

    filenames: list[str]
    total_job_count: int
    generated_at: datetime


class SitemapService:
    """Build and atomically publish a complete set of sitemap files."""

    def __init__(self, settings: SitemapSettings | None = None) -> None:
        self.settings = settings or SitemapSettings.from_environment()

    def generate_sitemaps(self) -> SitemapManifest:
        """Write active jobs into gzip chunks and one sitemap index."""
        generated_at = datetime.now(tz=UTC)
        output_dir = self.settings.output_dir.resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        chunk_filenames: list[str] = []
        total_job_count = 0
        after_id = 0

        build_dir = output_dir.parent / f".sitemap-build-{uuid4().hex}"
        build_dir.mkdir()
        try:
            with SessionLocal() as session:
                repository = SitemapRepository(session)
                while jobs := repository.list_active_jobs_after(
                    after_id=after_id,
                    limit=self.settings.chunk_size,
                ):
                    filename = f"sitemap-{len(chunk_filenames) + 1}.xml.gz"
                    self._write_urlset(build_dir / filename, jobs)
                    chunk_filenames.append(filename)
                    total_job_count += len(jobs)
                    after_id = jobs[-1].id

            self._write_index(
                build_dir / "sitemap.xml", chunk_filenames, generated_at
            )

            for filename in chunk_filenames:
                os.replace(build_dir / filename, output_dir / filename)
            os.replace(build_dir / "sitemap.xml", output_dir / "sitemap.xml")

            current_names = set(chunk_filenames)
            for stale_path in output_dir.glob("sitemap-*.xml.gz"):
                if stale_path.name not in current_names:
                    stale_path.unlink()
        finally:
            shutil.rmtree(build_dir, ignore_errors=True)

        manifest = SitemapManifest(
            filenames=["sitemap.xml", *chunk_filenames],
            total_job_count=total_job_count,
            generated_at=generated_at,
        )
        logger.info(
            "Sitemap generation completed",
            extra={
                "filenames": manifest.filenames,
                "total_job_count": manifest.total_job_count,
                "generated_at": manifest.generated_at.isoformat(),
            },
        )
        return manifest

    def _write_urlset(self, path: Path, jobs: list[SitemapJob]) -> None:
        root = Element("urlset", xmlns=SITEMAP_NAMESPACE)
        for job in jobs:
            url = SubElement(root, "url")
            SubElement(url, "loc").text = (
                f"{self.settings.public_site_base_url}/job/"
                f"{quote(job.slug, safe='')}"
            )
            SubElement(url, "lastmod").text = job.last_imported_at.isoformat()
        xml = tostring(root, encoding="utf-8", xml_declaration=True)
        with gzip.open(path, "wb") as sitemap_file:
            sitemap_file.write(xml)

    def _write_index(
        self, path: Path, chunk_filenames: list[str], generated_at: datetime
    ) -> None:
        root = Element("sitemapindex", xmlns=SITEMAP_NAMESPACE)
        for filename in chunk_filenames:
            sitemap = SubElement(root, "sitemap")
            SubElement(sitemap, "loc").text = (
                f"{self.settings.public_site_base_url}/{filename}"
            )
            SubElement(sitemap, "lastmod").text = generated_at.isoformat()
        path.write_bytes(tostring(root, encoding="utf-8", xml_declaration=True))


def generate_sitemaps() -> SitemapManifest:
    """Generate sitemap files using current environment settings."""
    return SitemapService().generate_sitemaps()
