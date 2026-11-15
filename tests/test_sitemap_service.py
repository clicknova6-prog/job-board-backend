from __future__ import annotations

import gzip
import shutil
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4
from xml.etree.ElementTree import fromstring

import pytest
from fastapi.testclient import TestClient

from app.api.routers.sitemaps import get_site_by_slug
from app.core.config import SitemapSettings
from app.db.repositories import SitemapJob, SitemapSite
from app.db.site_repositories import SiteRecord
from app.main import app
from app.services import sitemap_service

NS = {"sm": sitemap_service.SITEMAP_NAMESPACE}


class _SessionContext:
    def __enter__(self) -> object:
        return object()

    def __exit__(self, *args: object) -> None:
        return None


@pytest.fixture
def sitemap_output_dir() -> Iterator[Path]:
    path = Path("storage") / f"sitemap-test-{uuid4().hex}"
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path)


def _locs(path: Path, tag: str) -> list[str | None]:
    raw = path.read_bytes()
    if path.suffix == ".gz":
        raw = gzip.decompress(raw)
    return [e.text for e in fromstring(raw).findall(f"sm:{tag}/sm:loc", NS)]


def test_generates_chunked_sitemaps_per_site_and_removes_stale_files(
    sitemap_output_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    at = datetime(2026, 8, 20, tzinfo=UTC)
    jobs_by_site = {
        1: [
            SitemapJob(1, "python-&-sql", at),
            SitemapJob(2, "platform-engineer", at),
            SitemapJob(3, "data-engineer", at),
        ],
        2: [SitemapJob(3, "data-engineer", at)],
    }

    class FakeRepository:
        def __init__(self, session: object) -> None:
            pass

        def list_active_sites(self) -> list[SitemapSite]:
            return [
                SitemapSite(1, "it-tech", "https://it.example/"),
                SitemapSite(2, "medical", "https://med.example"),
            ]

        def list_active_jobs_after(
            self, *, site_id: int, canonical_priority: int, after_id: int, limit: int
        ) -> list[SitemapJob]:
            return [j for j in jobs_by_site[site_id] if j.id > after_id][:limit]

    monkeypatch.setattr(sitemap_service, "SessionLocal", _SessionContext)
    monkeypatch.setattr(sitemap_service, "SitemapRepository", FakeRepository)
    (sitemap_output_dir / "it-tech").mkdir()
    (sitemap_output_dir / "it-tech" / "sitemap-99.xml.gz").write_bytes(b"stale")
    settings = SitemapSettings(sitemap_output_dir, 2, 240)

    run = sitemap_service.SitemapService(settings).generate_sitemaps()

    it, med = run.sites
    assert (it.site_slug, it.total_job_count) == ("it-tech", 3)
    assert it.filenames == ["sitemap.xml", "sitemap-1.xml.gz", "sitemap-2.xml.gz"]
    assert (med.site_slug, med.total_job_count) == ("medical", 1)
    it_dir = sitemap_output_dir / "it-tech"
    assert not (it_dir / "sitemap-99.xml.gz").exists()
    assert not list(sitemap_output_dir.glob(".sitemap-build-*"))

    assert _locs(it_dir / "sitemap.xml", "sitemap") == [
        "https://it.example/sitemap-1.xml.gz",
        "https://it.example/sitemap-2.xml.gz",
    ]
    assert _locs(it_dir / "sitemap-1.xml.gz", "url") == [
        "https://it.example/job/python-%26-sql",
        "https://it.example/job/platform-engineer",
    ]
    assert _locs(sitemap_output_dir / "medical" / "sitemap-1.xml.gz", "url") == [
        "https://med.example/job/data-engineer",
    ]


@pytest.fixture
def it_site() -> Iterator[None]:
    site = SiteRecord(
        id=1, slug="it-tech", name="IT", base_url="https://it.example/", config={}
    )
    app.dependency_overrides[get_site_by_slug] = lambda: site
    yield
    app.dependency_overrides.pop(get_site_by_slug, None)


def test_sitemap_routes_serve_the_sites_files_and_return_404(
    sitemap_output_dir: Path, monkeypatch: pytest.MonkeyPatch, it_site: None
) -> None:
    monkeypatch.setenv("SITEMAP_OUTPUT_DIR", str(sitemap_output_dir))
    site_dir = sitemap_output_dir / "it-tech"
    site_dir.mkdir()
    (site_dir / "sitemap.xml").write_text("<sitemapindex/>", encoding="utf-8")
    (site_dir / "sitemap-1.xml.gz").write_bytes(b"gzip-content")
    client = TestClient(app)

    assert client.get("/sites/it-tech/sitemap.xml").status_code == 200
    assert client.get("/sites/it-tech/sitemap-1.xml.gz").content == b"gzip-content"
    assert client.get("/sites/it-tech/sitemap-2.xml.gz").status_code == 404
    assert client.get("/sites/it-tech/sitemap-invalid.xml.gz").status_code == 404


def test_robots_txt_points_at_the_sites_sitemap(it_site: None) -> None:
    response = TestClient(app).get("/sites/it-tech/robots.txt")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "Sitemap: https://it.example/sitemap.xml" in response.text


def test_site_slug_must_match_slug_format() -> None:
    assert TestClient(app).get("/sites/..%2Fetc/robots.txt").status_code in {404, 422}
    assert TestClient(app).get("/sites/Bad_Slug/robots.txt").status_code == 422
