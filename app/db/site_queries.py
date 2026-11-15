"""Shared SQL expressions for choosing a job's canonical (owning) site.

A job can be assigned to several sites. Exactly one active site owns its
canonical URL, sitemap entry and JSON-LD: the highest ``canonical_priority``,
ties broken by the lowest site id. Used by both the sync (sitemaps) and async
(public API) repositories, so the rule lives in one place.
"""

from __future__ import annotations

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import ColumnElement

from app.db.models import Job, JobSite, Site


def canonical_site_id_for_job() -> ColumnElement[int | None]:
    """Correlated scalar subquery: id of the job's canonical site (or NULL)."""
    return (
        select(Site.id)
        .join(JobSite, JobSite.site_id == Site.id)
        .where(JobSite.job_id == Job.id, Site.is_active.is_(True))
        .order_by(Site.canonical_priority.desc(), Site.id.asc())
        .limit(1)
        .correlate(Job)
        .scalar_subquery()
    )


def canonical_base_url_for_job() -> ColumnElement[str | None]:
    """Correlated scalar subquery: ``base_url`` of the job's canonical site."""
    return (
        select(Site.base_url)
        .where(Site.id == canonical_site_id_for_job())
        .correlate(Job)
        .scalar_subquery()
    )


def owned_by_site(site_id: int, canonical_priority: int) -> ColumnElement[bool]:
    """Predicate: no other active site of the job outranks ``site_id``."""
    other = aliased(Site)
    other_assignment = aliased(JobSite)
    return ~exists().where(
        other_assignment.job_id == Job.id,
        other.id == other_assignment.site_id,
        other.is_active.is_(True),
        other.id != site_id,
        or_(
            other.canonical_priority > canonical_priority,
            and_(other.canonical_priority == canonical_priority, other.id < site_id),
        ),
    )
