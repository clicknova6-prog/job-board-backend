"""Public job catalogue routes (v1)."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.cursors import InvalidCursorError, decode_cursor, encode_cursor
from app.db.async_session import get_async_session
from app.db.public_job_repositories import (
    JobKeysetCursor,
    JobSearchFilters,
    PublicJobRepository,
)
from app.schemas.job_public import JobListResponse, JobSummary

router = APIRouter()

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 20

# "-last_imported_at" selects ascending order, per the endpoint contract. Note
# this is the inverse of the more common "-" == descending convention.
SortOption = Literal["last_imported_at", "-last_imported_at"]


@router.get("/jobs", response_model=JobListResponse, summary="Search active jobs")
async def search_jobs(
    session: Annotated[AsyncSession, Depends(get_async_session)],
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
    """Return one keyset-paginated page of active jobs.

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

    repository = PublicJobRepository(session)
    # One extra row answers "is there another page?" without a COUNT.
    records = await repository.search(
        filters=JobSearchFilters(
            classification=classification,
            employment_type=employment_type,
            country_name=country_name,
            location=location,
            q=q,
        ),
        descending=sort == "last_imported_at",
        limit=limit + 1,
        cursor=keyset_cursor,
    )

    has_more = len(records) > limit
    page = records[:limit]

    next_cursor = None
    if has_more:
        last = page[-1]
        next_cursor = encode_cursor(
            JobKeysetCursor(last_imported_at=last.last_imported_at, id=last.id)
        )

    return JobListResponse(
        items=[JobSummary.model_validate(record) for record in page],
        next_cursor=next_cursor,
        has_more=has_more,
    )
