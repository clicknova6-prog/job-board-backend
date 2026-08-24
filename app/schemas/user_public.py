"""Public schemas for an authenticated job seeker's account."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class UserProfileRead(BaseModel):
    """The lean public profile for the current job seeker."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str
    display_name: str | None


class UserProfileUpdate(BaseModel):
    """Fields an authenticated job seeker may update."""

    model_config = ConfigDict(str_strip_whitespace=True)

    display_name: str


class SavedJobRead(BaseModel):
    """The persisted bookmark returned by a save operation."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    job_id: int
    saved_at: datetime
