"""FastAPI dependencies for auth services and authenticated principals."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.schemas import CurrentAdmin, CurrentUser
from app.core.auth_config import GoogleOAuthSettings, JWTSettings
from app.db.async_session import get_async_session
from app.db.auth_repositories import AuthRepository
from app.db.models import AdminRole
from app.services.auth.admin_auth_service import AdminAuthService
from app.services.auth.exceptions import InvalidAccessTokenError
from app.services.auth.google_oauth_service import GoogleOAuthService
from app.services.auth.jwt_service import JWTService

_bearer_scheme = HTTPBearer(auto_error=False)


def get_jwt_settings() -> JWTSettings:
    """Load JWT settings for one dependency graph."""
    return JWTSettings.from_environment()


def get_google_oauth_settings() -> GoogleOAuthSettings:
    """Load Google OAuth settings for one dependency graph."""
    return GoogleOAuthSettings.from_environment()


def get_auth_repository(
    session: Annotated[AsyncSession, Depends(get_async_session)],
) -> AuthRepository:
    """Create an auth repository around the request-scoped async session."""
    return AuthRepository(session)


def get_jwt_service(
    repository: Annotated[AuthRepository, Depends(get_auth_repository)],
    settings: Annotated[JWTSettings, Depends(get_jwt_settings)],
) -> JWTService:
    """Create the JWT service for the current request."""
    return JWTService(repository, settings=settings)


def get_google_oauth_service(
    repository: Annotated[AuthRepository, Depends(get_auth_repository)],
    settings: Annotated[GoogleOAuthSettings, Depends(get_google_oauth_settings)],
) -> GoogleOAuthService:
    """Create the Google OAuth service for the current request."""
    return GoogleOAuthService(repository, settings=settings)


def get_admin_auth_service(
    repository: Annotated[AuthRepository, Depends(get_auth_repository)],
) -> AdminAuthService:
    """Create the administrator authentication service for the current request."""
    return AdminAuthService(repository)


def _unauthorized() -> HTTPException:
    """Build the common bearer-token authentication failure."""
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing access token",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_user(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(_bearer_scheme),
    ],
    jwt_service: Annotated[JWTService, Depends(get_jwt_service)],
) -> CurrentUser:
    """Validate a bearer token and require a job-seeker subject."""
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _unauthorized()
    try:
        claims = jwt_service.verify_access_token(credentials.credentials)
        if claims.get("subject_type") != "user":
            raise InvalidAccessTokenError("Access token is not user-scoped")
        return CurrentUser(id=UUID(str(claims["subject_id"])))
    except (InvalidAccessTokenError, KeyError, ValueError) as error:
        raise _unauthorized() from error


async def get_current_admin(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(_bearer_scheme),
    ],
    jwt_service: Annotated[JWTService, Depends(get_jwt_service)],
) -> CurrentAdmin:
    """Validate a bearer token and require an administrator subject and role."""
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _unauthorized()
    try:
        claims = jwt_service.verify_access_token(credentials.credentials)
        if claims.get("subject_type") != "admin":
            raise InvalidAccessTokenError("Access token is not admin-scoped")
        return CurrentAdmin(
            id=UUID(str(claims["subject_id"])),
            role=AdminRole(str(claims["role"])),
        )
    except (InvalidAccessTokenError, KeyError, ValueError) as error:
        raise _unauthorized() from error


def require_admin_role(
    *allowed_roles: AdminRole,
) -> Callable[..., Coroutine[Any, Any, CurrentAdmin]]:
    """Build a dependency that additionally requires one of the given roles."""

    async def dependency(
        current_admin: Annotated[CurrentAdmin, Depends(get_current_admin)],
    ) -> CurrentAdmin:
        if current_admin.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient administrator role",
            )
        return current_admin

    return dependency
