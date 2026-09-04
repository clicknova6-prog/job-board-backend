"""Environment-backed application settings."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

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


def _redis_db_index(redis_url: str) -> int:
    parts = urlsplit(redis_url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    raw_db = query.get("db", parts.path.lstrip("/") or "0")
    try:
        db_index = int(raw_db)
    except ValueError as exc:
        raise RuntimeError("Redis URL must contain a numeric database index") from exc
    if db_index < 0:
        raise RuntimeError("Redis database index cannot be negative")
    return db_index


def _redis_url_with_db(redis_url: str, db_index: int) -> str:
    parts = urlsplit(redis_url)
    if parts.scheme not in {"redis", "rediss"}:
        raise RuntimeError("REDIS_BROKER_URL must use redis:// or rediss://")
    query = urlencode(
        [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if key.lower() != "db"
        ]
    )
    return urlunsplit(parts._replace(path=f"/{db_index}", query=query))


@dataclass(frozen=True, slots=True)
class RateLimitSettings:
    """Redis storage and per-route rate-limit configuration."""

    storage_uri: str
    public_jobs: str
    google_auth: str
    public_auth_refresh: str
    admin_auth_login: str
    admin_auth_refresh: str
    admin_api: str
    admin_import_trigger: str
    affiliate_redirect: str

    @classmethod
    def from_environment(cls) -> RateLimitSettings:
        """Build a Redis URI on a DB distinct from the Celery broker."""
        broker_url = os.environ.get(
            "REDIS_BROKER_URL",
            "redis://localhost:6379/0",
        )
        broker_db = _redis_db_index(broker_url)
        ratelimit_db = int(os.environ.get("REDIS_RATELIMIT_DB", "2"))
        if ratelimit_db < 0:
            raise RuntimeError("REDIS_RATELIMIT_DB cannot be negative")
        if ratelimit_db == broker_db:
            raise RuntimeError(
                "REDIS_RATELIMIT_DB must differ from the Celery broker DB"
            )

        return cls(
            storage_uri=_redis_url_with_db(broker_url, ratelimit_db),
            public_jobs=os.environ.get("RATE_LIMIT_PUBLIC_JOBS", "300/minute"),
            google_auth=os.environ.get("RATE_LIMIT_GOOGLE_AUTH", "5/minute"),
            public_auth_refresh=os.environ.get(
                "RATE_LIMIT_PUBLIC_AUTH_REFRESH",
                "30/minute",
            ),
            admin_auth_login=os.environ.get(
                "RATE_LIMIT_ADMIN_AUTH_LOGIN",
                "5/minute",
            ),
            admin_auth_refresh=os.environ.get(
                "RATE_LIMIT_ADMIN_AUTH_REFRESH",
                "30/minute",
            ),
            admin_api=os.environ.get("RATE_LIMIT_ADMIN_API", "120/minute"),
            admin_import_trigger=os.environ.get(
                "RATE_LIMIT_ADMIN_IMPORT_TRIGGER",
                "5/minute",
            ),
            affiliate_redirect=os.environ.get(
                "RATE_LIMIT_AFFILIATE_REDIRECT",
                "600/minute",
            ),
        )


@dataclass(frozen=True, slots=True)
class FiltersCacheSettings:
    """Redis storage configuration for the cached /jobs/filters response."""

    storage_uri: str
    ttl_seconds: int

    @classmethod
    def from_environment(cls) -> FiltersCacheSettings:
        """Build a Redis URI on a DB distinct from the broker and rate-limit DBs."""
        broker_url = os.environ.get(
            "REDIS_BROKER_URL",
            "redis://localhost:6379/0",
        )
        broker_db = _redis_db_index(broker_url)
        ratelimit_db = int(os.environ.get("REDIS_RATELIMIT_DB", "2"))
        cache_db = int(os.environ.get("REDIS_FILTERS_CACHE_DB", "3"))
        if cache_db < 0:
            raise RuntimeError("REDIS_FILTERS_CACHE_DB cannot be negative")
        if cache_db == broker_db:
            raise RuntimeError(
                "REDIS_FILTERS_CACHE_DB must differ from the Celery broker DB"
            )
        if cache_db == ratelimit_db:
            raise RuntimeError(
                "REDIS_FILTERS_CACHE_DB must differ from the rate-limit DB"
            )

        return cls(
            storage_uri=_redis_url_with_db(broker_url, cache_db),
            ttl_seconds=_positive_int("FILTERS_CACHE_TTL_SECONDS", 6 * 60 * 60),
        )


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
            output_dir=Path(os.environ.get("SITEMAP_OUTPUT_DIR", "storage/sitemaps")),
            public_site_base_url=base_url,
            chunk_size=chunk_size,
            regen_interval_minutes=_positive_int("SITEMAP_REGEN_INTERVAL_MINUTES", 240),
        )
