"""Request, response, and authenticated-principal schemas."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.db.models import AdminRole


class AccessTokenResponse(BaseModel):
    """Access token returned in an authentication response body."""

    access_token: str
    token_type: str = "bearer"


class AdminLoginRequest(BaseModel):
    """Administrator email/password credentials."""

    model_config = ConfigDict(str_strip_whitespace=True)

    email: str
    password: str


class CurrentUser(BaseModel):
    """Validated job-seeker access-token identity."""

    id: UUID


class CurrentAdmin(BaseModel):
    """Validated administrator access-token identity and role."""

    id: UUID
    role: AdminRole
