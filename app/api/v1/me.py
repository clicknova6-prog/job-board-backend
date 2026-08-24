"""Authenticated job-seeker profile and saved-job routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.auth.schemas import CurrentUser
from app.db.async_session import get_async_session
from app.db.public_job_repositories import JobSummaryRecord
from app.db.saved_job_repositories import (
    SavedJobRecord,
    SavedJobRepository,
    UserProfileRecord,
)
from app.schemas.job_public import JobSummary
from app.schemas.user_public import SavedJobRead, UserProfileRead, UserProfileUpdate

router = APIRouter(
    prefix="/me",
    tags=["Me"],
    dependencies=[Depends(get_current_user)],
)


@router.get("", response_model=UserProfileRead)
async def get_me(
    session: Annotated[AsyncSession, Depends(get_async_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> UserProfileRecord:
    """Return the authenticated job seeker's lean profile."""
    profile = await SavedJobRepository(session).get_user_profile(current_user.id)
    if profile is None:
        raise _user_not_found()
    return profile


@router.patch("", response_model=UserProfileRead)
async def update_me(
    update: UserProfileUpdate,
    session: Annotated[AsyncSession, Depends(get_async_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> UserProfileRecord:
    """Update the authenticated job seeker's display name."""
    profile = await SavedJobRepository(session).update_display_name(
        current_user.id,
        update.display_name,
    )
    if profile is None:
        raise _user_not_found()
    await session.commit()
    return profile


@router.get("/saved-jobs", response_model=list[JobSummary])
async def list_saved_jobs(
    session: Annotated[AsyncSession, Depends(get_async_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> list[JobSummaryRecord]:
    """Return the authenticated job seeker's active saved jobs."""
    return await SavedJobRepository(session).list_saved_jobs(current_user.id)


@router.post("/saved-jobs/{job_id}", response_model=SavedJobRead)
async def save_job(
    job_id: int,
    session: Annotated[AsyncSession, Depends(get_async_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> SavedJobRecord:
    """Idempotently save one active job for the authenticated job seeker."""
    saved_job = await SavedJobRepository(session).save_job(current_user.id, job_id)
    if saved_job is None:
        raise _job_not_found()
    await session.commit()
    return saved_job


@router.delete("/saved-jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_saved_job(
    job_id: int,
    session: Annotated[AsyncSession, Depends(get_async_session)],
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> Response:
    """Idempotently remove one saved job for the authenticated job seeker."""
    await SavedJobRepository(session).delete_saved_job(current_user.id, job_id)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _user_not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="User not found",
    )


def _job_not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Job not found",
    )
