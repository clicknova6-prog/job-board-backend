"""Public job catalogue routes (v1)."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.site_dependencies import CurrentSite
from app.api.v1.cursors import InvalidCursorError, decode_cursor, encode_cursor
from app.core.filters_cache import filters_cache
from app.core.rate_limit import limiter, rate_limit_settings
from app.db.async_session import get_async_session
from app.db.public_job_repositories import (
    JobKeysetCursor,
    JobSearchFilters,
    PublicJobRepository,
)
from app.schemas.job_public import (
    JobDetail,
    JobFilterMetadataOut,
    JobListResponse,
    JobSummary,
)
from app.services.sitemap_service import job_canonical_url
from app.services.structured_data_service import build_job_posting_ld

router = APIRouter()

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 20
MAX_TRAVERSABLE_RESULTS = 10_000

# "-last_imported_at" selects ascending order, per the endpoint contract. Note
# this is the inverse of the more common "-" == descending convention.
SortOption = Literal["last_imported_at", "-last_imported_at"]


@router.get("/jobs", response_model=JobListResponse, summary="Search active jobs")
@limiter.limit(rate_limit_settings.public_jobs)
async def search_jobs(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_async_session)],
    site: CurrentSite,
    classification: Annotated[str | None, Query()] = None,
    employment_type: Annotated[str | None, Query()] = None,
    country_name: Annotated[str | None, Query()] = None,
    location: Annotated[str | None, Query()] = None,
    q: Annotated[
        str | None, Query(description="Full-text search over title and description")
    ] = None,
    sort: Annotated[SortOption, Query()] = "last_imported_at",
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[
        str | None, Query(description="Opaque page token from a previous response")
    ] = None,
) -> JobListResponse:
    """Return one keyset-paginated page of the calling site's active jobs.

    Inactive (soft-deleted) jobs are never returned. Paging is keyset-based on
    ``(last_imported_at, id)``; OFFSET is intentionally not used so page
    contents stay stable while imports run.
    """
    keyset_cursor: JobKeysetCursor | None = None
    if cursor is not None:
        try:
            keyset_cursor = decode_cursor(cursor)
        except InvalidCursorError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid cursor: {exc}",
            ) from exc

    served_count = keyset_cursor.served_count if keyset_cursor is not None else 0
    page_limit = min(limit, max(MAX_TRAVERSABLE_RESULTS - served_count, 0))

    records = []
    if page_limit:
        repository = PublicJobRepository(session)
        # One extra row answers "is there another page?" without a COUNT.
        records = await repository.search(
            site_id=site.id,
            filters=JobSearchFilters(
                classification=classification,
                employment_type=employment_type,
                country_name=country_name,
                location=location,
                q=q,
            ),
            descending=sort == "last_imported_at",
            limit=page_limit + 1,
            cursor=keyset_cursor,
        )

    page = records[:page_limit]
    served_count += len(page)
    has_more = len(records) > page_limit and served_count < MAX_TRAVERSABLE_RESULTS

    next_cursor = None
    if has_more:
        last = page[-1]
        next_cursor = encode_cursor(
            JobKeysetCursor(
                last_imported_at=last.last_imported_at,
                id=last.id,
                served_count=served_count,
            )
        )

    return JobListResponse(
        items=[JobSummary.model_validate(record) for record in page],
        next_cursor=next_cursor,
        has_more=has_more,
    )


@router.get(
    "/jobs/filters",
    response_model=JobFilterMetadataOut,
    summary="List available job filters",
)
@limiter.limit(rate_limit_settings.public_jobs)
async def get_job_filters(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_async_session)],
    site: CurrentSite,
) -> JobFilterMetadataOut:
    """Return filter values that occur on at least one of the site's active jobs.

    Served from a versioned Redis cache when available; a cache miss (or an
    unreachable Redis) runs the aggregate query directly.
    """

    async def compute() -> dict[str, Any]:
        metadata = await PublicJobRepository(session).get_filter_metadata(
            site_id=site.id
        )
        return JobFilterMetadataOut.model_validate(metadata).model_dump(mode="json")

    data = await filters_cache.get_or_compute(compute, scope=f"site:{site.id}")
    return JobFilterMetadataOut.model_validate(data)


@router.get("/jobs/{slug}", response_model=JobDetail, summary="Get a job by slug")
@limiter.limit(rate_limit_settings.public_jobs)
async def get_job_detail(
    slug: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_async_session)],
    site: CurrentSite,
) -> JobDetail:
    """Return an active or soft-deleted job of the calling site by its slug."""
    record = await PublicJobRepository(session).get_by_slug(slug, site_id=site.id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found",
        )

    # A job shared by several sites has one canonical owner; other sites point
    # their canonical at it and leave JSON-LD to the owner (no duplicates).
    is_owner = record.canonical_site_id in (None, site.id)
    canonical_url = job_canonical_url(
        site.base_url if is_owner else record.canonical_base_url, record.slug
    )
    return JobDetail(
        id=record.id,
        slug=record.slug,
        title=record.title,
        classification=record.classification,
        employment_type=record.employment_type,
        country_name=record.country_name,
        location=record.location,
        apply_url=record.apply_url,
        first_imported_at=record.first_imported_at,
        last_imported_at=record.last_imported_at,
        is_active=record.is_active,
        remote_status=record.remote_status,
        remote_status_source=record.remote_status_source,
        experience_level=record.experience_level,
        experience_level_source=record.experience_level_source,
        description=record.description,
        advertiser_name=record.advertiser_name,
        salary_min=record.salary_min,
        salary_max=record.salary_max,
        salary_currency=record.salary_currency,
        salary_period=record.salary_period,
        created_at=record.created_at,
        content_updated_at=record.content_updated_at,
        canonical_url=canonical_url,
        structured_data=(
            build_job_posting_ld(record, canonical_url=canonical_url)
            if is_owner
            else None
        ),
    )
