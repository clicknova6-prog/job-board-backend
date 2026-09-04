"""Administrator authentication routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse, Response

from app.auth.cookies import (
    ADMIN_REFRESH_COOKIE,
    ADMIN_REFRESH_PATH,
    clear_refresh_cookie,
    set_refresh_cookie,
)
from app.auth.dependencies import get_admin_auth_service, get_jwt_service
from app.auth.schemas import AccessTokenResponse, AdminLoginRequest
from app.core.errors import error_content
from app.core.rate_limit import limiter
from app.services.auth.admin_auth_service import AdminAuthService
from app.services.auth.exceptions import (
    AuthSubjectDisabledError,
    InvalidAdminCredentialsError,
    InvalidRefreshTokenError,
)
from app.services.auth.jwt_service import JWTService

router = APIRouter(prefix="/admin/auth", tags=["Admin Auth"])


def _access_token_response(access_token: str) -> JSONResponse:
    """Build the standard access-token JSON response."""
    content = AccessTokenResponse(access_token=access_token).model_dump()
    return JSONResponse(content=content)


def _invalid_credentials_response() -> JSONResponse:
    """Build the generic administrator authentication failure."""
    return JSONResponse(
        status_code=status.HTTP_401_UNAUTHORIZED,
        content=error_content(
            code="UNAUTHORIZED",
            message="invalid credentials",
        ),
    )


@router.post("/login", response_model=None)
@limiter.limit("5/minute")
async def admin_login(
    request: Request,
    credentials: AdminLoginRequest,
    admin_service: Annotated[AdminAuthService, Depends(get_admin_auth_service)],
    jwt_service: Annotated[JWTService, Depends(get_jwt_service)],
) -> JSONResponse:
    """Authenticate an administrator and issue access and refresh tokens."""
    try:
        admin = await admin_service.authenticate_admin(
            credentials.email,
            credentials.password,
        )
    except InvalidAdminCredentialsError:
        return _invalid_credentials_response()

    access_token = jwt_service.issue_access_token(
        admin.id,
        "admin",
        role=admin.role,
    )
    refresh_token = await jwt_service.issue_refresh_token(admin.id, "admin")
    response = _access_token_response(access_token)
    set_refresh_cookie(
        response,
        cookie_name=ADMIN_REFRESH_COOKIE,
        cookie_path=ADMIN_REFRESH_PATH,
        raw_token=refresh_token,
    )
    return response


@router.post("/refresh", response_model=None)
@limiter.limit("30/minute")
async def refresh_admin_access_token(
    request: Request,
    jwt_service: Annotated[JWTService, Depends(get_jwt_service)],
) -> JSONResponse:
    """Rotate an administrator refresh token and return a new access token."""
    raw_token = request.cookies.get(ADMIN_REFRESH_COOKIE)
    if raw_token is None:
        return _invalid_credentials_response()
    try:
        access_token, refresh_token = await jwt_service.rotate_refresh_token(
            raw_token,
            expected_subject_type="admin",
        )
    except (InvalidRefreshTokenError, AuthSubjectDisabledError):
        response = _invalid_credentials_response()
        clear_refresh_cookie(
            response,
            cookie_name=ADMIN_REFRESH_COOKIE,
            cookie_path=ADMIN_REFRESH_PATH,
        )
        return response

    response = _access_token_response(access_token)
    set_refresh_cookie(
        response,
        cookie_name=ADMIN_REFRESH_COOKIE,
        cookie_path=ADMIN_REFRESH_PATH,
        raw_token=refresh_token,
    )
    return response


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def admin_logout(
    request: Request,
    jwt_service: Annotated[JWTService, Depends(get_jwt_service)],
) -> Response:
    """Revoke and clear an administrator refresh token."""
    raw_token = request.cookies.get(ADMIN_REFRESH_COOKIE)
    if raw_token is not None:
        try:
            await jwt_service.revoke_refresh_token(
                raw_token,
                expected_subject_type="admin",
            )
        except InvalidRefreshTokenError:
            pass
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    clear_refresh_cookie(
        response,
        cookie_name=ADMIN_REFRESH_COOKIE,
        cookie_path=ADMIN_REFRESH_PATH,
    )
    return response
