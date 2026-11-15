"""Async read-side persistence for public site resolution.

Public API requests identify their niche site with the ``X-Site-Key`` header;
this module resolves that key to a plain frozen record so routers and
middleware never touch ORM objects.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Site


def origin_of(url: str) -> str | None:
    """Return the ``scheme://host[:port]`` origin of ``url``, or None if invalid."""
    parts = urlsplit(url.strip())
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}".lower()


def site_allowed_origins(base_url: str, config: dict[str, Any]) -> frozenset[str]:
    """Browser origins allowed to call the API on behalf of one site.

    The site's own ``base_url`` origin is always allowed; ``config.cors_origins``
    adds extras (www/apex variants, preview deployments, local dev).
    """
    candidates = [base_url, *(config.get("cors_origins") or [])]
    return frozenset(
        origin
        for origin in (
            origin_of(value) for value in candidates if isinstance(value, str)
        )
        if origin is not None
    )


@dataclass(frozen=True, slots=True)
class SiteRecord:
    """One active public site, as seen by the request layer."""

    id: int
    slug: str
    name: str
    base_url: str
    config: dict[str, Any]

    @property
    def allowed_origins(self) -> frozenset[str]:
        return site_allowed_origins(self.base_url, self.config)

    @property
    def canonical_base_url(self) -> str:
        """``base_url`` without a trailing slash, for building absolute URLs."""
        return self.base_url.rstrip("/")


class PublicSiteRepository:
    """Reads active sites for request scoping and CORS."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_active_by_key(self, site_key: str) -> SiteRecord | None:
        """Return the active site identified by ``site_key``, if any."""
        row = (
            (
                await self._session.execute(
                    select(
                        Site.id, Site.slug, Site.name, Site.base_url, Site.config
                    ).where(Site.site_key == site_key, Site.is_active.is_(True))
                )
            )
            .mappings()
            .one_or_none()
        )
        return SiteRecord(**row) if row is not None else None

    async def list_active_allowed_origins(self) -> frozenset[str]:
        """Return the union of allowed browser origins across active sites."""
        rows = await self._session.execute(
            select(Site.base_url, Site.config).where(Site.is_active.is_(True))
        )
        origins: set[str] = set()
        for base_url, config in rows:
            origins |= site_allowed_origins(base_url, config or {})
        return frozenset(origins)
