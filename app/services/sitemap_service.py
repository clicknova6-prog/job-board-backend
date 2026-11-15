"""Generate crawler-facing sitemap files, one set per active site."""

from __future__ import annotations

import gzip
import logging
import os
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4
from xml.etree.ElementTree import Element, SubElement, tostring

from app.core.config import SitemapSettings
from app.db.repositories import SitemapJob, SitemapRepository, SitemapSite
from app.db.session import SessionLocal

logger = logging.getLogger(__name__)
SITEMAP_NAMESPACE = "http://www.sitemaps.org/schemas/sitemap/0.9"
JOB_PATH_PREFIX = "/job/"


def job_canonical_url(base_url: str, slug: str) -> str:
    """The one public URL of a job on a site (sitemaps, canonical, JSON-LD)."""
    return f"{base_url.rstrip('/')}{JOB_PATH_PREFIX}{quote(slug, safe='')}"


@dataclass(frozen=True, slots=True)
class SitemapManifest:
    """What was published for one site."""

    site_slug: str
    filenames: list[str]
    total_job_count: int


@dataclass(frozen=True, slots=True)
class SitemapRunManifest:
    """Small generation result for all sites, suitable for task logging."""

    generated_at: datetime
    sites: list[SitemapManifest] = field(default_factory=list)


class SitemapService:
    """Build and atomically publish a complete sitemap set for every site."""

    def __init__(self, settings: SitemapSettings | None = None) -> None:
        self.settings = settings or SitemapSettings.from_environment()

    def generate_sitemaps(self) -> SitemapRunManifest:
        """Write each active site's jobs into gzip chunks plus a sitemap index."""
        run = SitemapRunManifest(generated_at=datetime.now(tz=UTC))
        with SessionLocal() as session:
            repository = SitemapRepository(session)
            for site in repository.list_active_sites():
                run.sites.append(
                    self._generate_site(repository, site, run.generated_at)
                )
        logger.info(
            "Sitemap generation completed",
            extra={
                "sites": {m.site_slug: m.total_job_count for m in run.sites},
                "generated_at": run.generated_at.isoformat(),
            },
        )
        return run

    def _generate_site(
        self, repository: SitemapRepository, site: SitemapSite, generated_at: datetime
    ) -> SitemapManifest:
        output_dir = self.settings.output_dir.resolve() / site.slug
        output_dir.mkdir(parents=True, exist_ok=True)
        chunk_filenames: list[str] = []
        total_job_count = 0
        after_id = 0

        build_dir = output_dir.parent / f".sitemap-build-{uuid4().hex}"
        build_dir.mkdir()
        try:
            while jobs := repository.list_active_jobs_after(
                site_id=site.id,
                canonical_priority=site.canonical_priority,
                after_id=after_id,
                limit=self.settings.chunk_size,
            ):
                filename = f"sitemap-{len(chunk_filenames) + 1}.xml.gz"
                self._write_urlset(build_dir / filename, site.base_url, jobs)
                chunk_filenames.append(filename)
                total_job_count += len(jobs)
                after_id = jobs[-1].id

            self._write_index(
                build_dir / "sitemap.xml", site.base_url, chunk_filenames, generated_at
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

        return SitemapManifest(
            site_slug=site.slug,
            filenames=["sitemap.xml", *chunk_filenames],
            total_job_count=total_job_count,
        )

    @staticmethod
    def _write_urlset(path: Path, base_url: str, jobs: list[SitemapJob]) -> None:
        root = Element("urlset", xmlns=SITEMAP_NAMESPACE)
        for job in jobs:
            url = SubElement(root, "url")
            SubElement(url, "loc").text = job_canonical_url(base_url, job.slug)
            SubElement(url, "lastmod").text = job.last_imported_at.isoformat()
        xml = tostring(root, encoding="utf-8", xml_declaration=True)
        with gzip.open(path, "wb") as sitemap_file:
            sitemap_file.write(xml)

    @staticmethod
    def _write_index(
        path: Path, base_url: str, chunk_filenames: list[str], generated_at: datetime
    ) -> None:
        root = Element("sitemapindex", xmlns=SITEMAP_NAMESPACE)
        for filename in chunk_filenames:
            sitemap = SubElement(root, "sitemap")
            SubElement(sitemap, "loc").text = f"{base_url.rstrip('/')}/{filename}"
            SubElement(sitemap, "lastmod").text = generated_at.isoformat()
        path.write_bytes(tostring(root, encoding="utf-8", xml_declaration=True))


def generate_sitemaps() -> SitemapRunManifest:
    """Generate sitemap files for every active site."""
    return SitemapService().generate_sitemaps()
