from __future__ import annotations

import gzip
import shutil
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4
from xml.etree.ElementTree import fromstring

import pytest
from fastapi.testclient import TestClient

from app.core.config import SitemapSettings
from app.db.repositories import SitemapJob
from app.main import app
from app.services import sitemap_service


class _SessionContext:
    def __enter__(self) -> object:
        return object()

    def __exit__(self, *args: object) -> None:
        return None


@pytest.fixture
def sitemap_output_dir() -> Path:
    path = Path("storage") / f"sitemap-test-{uuid4().hex}"
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path)


def test_generates_chunked_sitemaps_and_removes_stale_files(
    sitemap_output_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs = [
        SitemapJob(1, "python-&-sql", datetime(2026, 8, 20, tzinfo=UTC)),
        SitemapJob(2, "platform-engineer", datetime(2026, 8, 21, tzinfo=UTC)),
        SitemapJob(3, "data-engineer", datetime(2026, 8, 22, tzinfo=UTC)),
    ]

    class FakeRepository:
        def __init__(self, session: object) -> None:
            pass

        def list_active_jobs_after(
            self, *, after_id: int, limit: int
        ) -> list[SitemapJob]:
            return [job for job in jobs if job.id > after_id][:limit]

    monkeypatch.setattr(sitemap_service, "SessionLocal", _SessionContext)
    monkeypatch.setattr(sitemap_service, "SitemapRepository", FakeRepository)
    (sitemap_output_dir / "sitemap-99.xml.gz").write_bytes(b"stale")
    settings = SitemapSettings(sitemap_output_dir, "https://jobs.example", 2, 240)

    manifest = sitemap_service.SitemapService(settings).generate_sitemaps()

    assert manifest.filenames == [
        "sitemap.xml",
        "sitemap-1.xml.gz",
        "sitemap-2.xml.gz",
    ]
    assert manifest.total_job_count == 3
    assert not (sitemap_output_dir / "sitemap-99.xml.gz").exists()

    index = fromstring((sitemap_output_dir / "sitemap.xml").read_bytes())
    namespace = {"sm": sitemap_service.SITEMAP_NAMESPACE}
    assert [element.text for element in index.findall("sm:sitemap/sm:loc", namespace)] == [
        "https://jobs.example/sitemap-1.xml.gz",
        "https://jobs.example/sitemap-2.xml.gz",
    ]

    with gzip.open(sitemap_output_dir / "sitemap-1.xml.gz", "rb") as sitemap_file:
        first_chunk = fromstring(sitemap_file.read())
    assert [element.text for element in first_chunk.findall("sm:url/sm:loc", namespace)] == [
        "https://jobs.example/job/python-%26-sql",
        "https://jobs.example/job/platform-engineer",
    ]


def test_sitemap_routes_serve_files_and_return_404(
    sitemap_output_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SITEMAP_OUTPUT_DIR", str(sitemap_output_dir))
    (sitemap_output_dir / "sitemap.xml").write_text(
        "<sitemapindex/>", encoding="utf-8"
    )
    (sitemap_output_dir / "sitemap-1.xml.gz").write_bytes(b"gzip-content")
    client = TestClient(app)

    assert client.get("/sitemap.xml").status_code == 200
    assert client.get("/sitemap-1.xml.gz").content == b"gzip-content"
    assert client.get("/sitemap-2.xml.gz").status_code == 404
    assert client.get("/sitemap-invalid.xml.gz").status_code == 404


def test_robots_txt_uses_configured_public_site_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PUBLIC_SITE_BASE_URL", "https://configured.example")

    response = TestClient(app).get("/robots.txt")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "Sitemap: https://configured.example/sitemap.xml" in response.text
