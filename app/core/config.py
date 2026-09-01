"""Environment-backed application settings."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env", encoding="utf-8-sig")


def _comma_separated_list(name: str, default: list[str]) -> list[str]:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    return [value.strip() for value in raw_value.split(",") if value.strip()]


CORS_ALLOWED_ORIGINS = _comma_separated_list(
    "CORS_ALLOWED_ORIGINS",
    ["http://localhost:3000"],
)


def _positive_int(name: str, default: int) -> int:
    value = int(os.environ.get(name, str(default)))
    if value <= 0:
        raise RuntimeError(f"{name} must be greater than zero")
    return value


@dataclass(frozen=True, slots=True)
class SitemapSettings:
    """Configuration used to generate and serve sitemap files."""

    output_dir: Path
    public_site_base_url: str
    chunk_size: int
    regen_interval_minutes: int

    @classmethod
    def from_environment(cls) -> SitemapSettings:
        """Load sitemap settings, requiring an explicit public site URL."""
        base_url = os.environ.get("PUBLIC_SITE_BASE_URL", "").strip().rstrip("/")
        if not base_url:
            raise RuntimeError("PUBLIC_SITE_BASE_URL must be configured")
        if not base_url.startswith(("http://", "https://")):
            raise RuntimeError("PUBLIC_SITE_BASE_URL must be an absolute HTTP(S) URL")

        chunk_size = _positive_int("SITEMAP_CHUNK_SIZE", 50_000)
        if chunk_size > 50_000:
            raise RuntimeError("SITEMAP_CHUNK_SIZE cannot exceed 50000")

        return cls(
            output_dir=Path(
                os.environ.get("SITEMAP_OUTPUT_DIR", "storage/sitemaps")
            ),
            public_site_base_url=base_url,
            chunk_size=chunk_size,
            regen_interval_minutes=_positive_int(
                "SITEMAP_REGEN_INTERVAL_MINUTES", 240
            ),
        )
