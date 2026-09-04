"""Public job-seeker authentication routes."""

from __future__ import annotations

import secrets
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse, RedirectResponse, Response

from app.auth.cookies import (
    OAUTH_STATE_COOKIE,
    OAUTH_STATE_MAX_AGE,
    OAUTH_STATE_PATH,
    PUBLIC_REFRESH_COOKIE,
    PUBLIC_REFRESH_PATH,
    clear_oauth_state_cookie,
    clear_refresh_cookie,
    set_refresh_cookie,
)
from app.auth.dependencies import (
    get_google_oauth_service,
    get_google_oauth_settings,
    get_jwt_service,
)
from app.auth.schemas import AccessTokenResponse
from app.core.auth_config import GoogleOAuthSettings
from app.core.errors import error_code, error_content
from app.core.rate_limit import limiter
from app.services.auth.exceptions import (
    AuthSubjectDisabledError,
    GoogleOAuthExchangeError,
    InvalidRefreshTokenError,
    OAuthEmailCollisionError,
)
from app.services.auth.google_oauth_service import GoogleOAuthService
from app.services.auth.jwt_service import JWTService

GOOGLE_AUTHORIZATION_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_SCOPES = "openid email profile"

router = APIRouter(prefix="/auth", tags=["Auth"])


def _access_token_response(access_token: str) -> JSONResponse:
    """Build the standard access-token JSON response."""
    content = AccessTokenResponse(access_token=access_token).model_dump()
    return JSONResponse(content=content)


def _error_response(status_code: int, detail: str) -> JSONResponse:
    """Build a non-leaking authentication error response."""
    return JSONResponse(
        status_code=status_code,
        content=error_content(code=error_code(status_code), message=detail),
    )


@router.get("/google/login", response_model=None)
@limiter.limit("5/minute")
async def google_login(
    request: Request,
    settings: Annotated[GoogleOAuthSettings, Depends(get_google_oauth_settings)],
) -> RedirectResponse:
    """Redirect a browser to Google's OAuth consent screen."""
    state = secrets.token_urlsafe(32)
    query = urlencode(
        {
            "client_id": settings.client_id,
            "redirect_uri": settings.redirect_uri,
            "response_type": "code",
            "scope": GOOGLE_SCOPES,
            "state": state,
        }
    )
    response = RedirectResponse(
        url=f"{GOOGLE_AUTHORIZATION_URL}?{query}",
        status_code=status.HTTP_302_FOUND,
    )
    response.set_cookie(
        key=OAUTH_STATE_COOKIE,
        value=state,
        max_age=OAUTH_STATE_MAX_AGE,
        path=OAUTH_STATE_PATH,
        secure=True,
        httponly=True,
        samesite="lax",
    )
    return response


@router.get("/google/callback", response_model=None)
@limiter.limit("5/minute")
async def google_callback(
    request: Request,
    google_service: Annotated[GoogleOAuthService, Depends(get_google_oauth_service)],
    jwt_service: Annotated[JWTService, Depends(get_jwt_service)],
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> JSONResponse:
    """Complete Google OAuth and return an access token."""
    stored_state = request.cookies.get(OAUTH_STATE_COOKIE)
    state_is_valid = bool(
        state and stored_state and secrets.compare_digest(state, stored_state)
    )
    if error is not None:
        response = _error_response(
            status.HTTP_400_BAD_REQUEST,
            "Google authorization was not completed",
        )
        clear_oauth_state_cookie(response)
        return response
    if not state_is_valid or not code:
        response = _error_response(
            status.HTTP_400_BAD_REQUEST,
            "Invalid Google OAuth callback",
        )
        clear_oauth_state_cookie(response)
        return response

    try:
        (
            email,
            oauth_subject_id,
            display_name,
        ) = await google_service.exchange_code_for_profile(code)
        user = await google_service.get_or_create_user(
            email,
            oauth_subject_id,
            display_name,
        )
        access_token = jwt_service.issue_access_token(user.id, "user")
        refresh_token = await jwt_service.issue_refresh_token(user.id, "user")
    except OAuthEmailCollisionError:
        response = _error_response(
            status.HTTP_409_CONFLICT,
            "An account already exists for this email",
        )
        clear_oauth_state_cookie(response)
        return response
    except GoogleOAuthExchangeError:
        response = _error_response(
            status.HTTP_502_BAD_GATEWAY,
            "Google authentication failed",
        )
        clear_oauth_state_cookie(response)
        return response

    response = _access_token_response(access_token)
    clear_oauth_state_cookie(response)
    set_refresh_cookie(
        response,
        cookie_name=PUBLIC_REFRESH_COOKIE,
        cookie_path=PUBLIC_REFRESH_PATH,
        raw_token=refresh_token,
    )
    return response


@router.post("/refresh", response_model=None)
@limiter.limit("30/minute")
async def refresh_access_token(
    request: Request,
    jwt_service: Annotated[JWTService, Depends(get_jwt_service)],
) -> JSONResponse:
    """Rotate a public refresh token and return a new access token."""
    raw_token = request.cookies.get(PUBLIC_REFRESH_COOKIE)
    if raw_token is None:
        return _error_response(
            status.HTTP_401_UNAUTHORIZED,
            "Invalid refresh token",
        )
    try:
        access_token, refresh_token = await jwt_service.rotate_refresh_token(
            raw_token,
            expected_subject_type="user",
        )
    except (InvalidRefreshTokenError, AuthSubjectDisabledError):
        response = _error_response(
            status.HTTP_401_UNAUTHORIZED,
            "Invalid refresh token",
        )
        clear_refresh_cookie(
            response,
            cookie_name=PUBLIC_REFRESH_COOKIE,
            cookie_path=PUBLIC_REFRESH_PATH,
        )
        return response

    response = _access_token_response(access_token)
    set_refresh_cookie(
        response,
        cookie_name=PUBLIC_REFRESH_COOKIE,
        cookie_path=PUBLIC_REFRESH_PATH,
        raw_token=refresh_token,
    )
    return response


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    jwt_service: Annotated[JWTService, Depends(get_jwt_service)],
) -> Response:
    """Revoke and clear a public refresh token."""
    raw_token = request.cookies.get(PUBLIC_REFRESH_COOKIE)
    if raw_token is not None:
        try:
            await jwt_service.revoke_refresh_token(
                raw_token,
                expected_subject_type="user",
            )
        except InvalidRefreshTokenError:
            pass
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    clear_refresh_cookie(
        response,
        cookie_name=PUBLIC_REFRESH_COOKIE,
        cookie_path=PUBLIC_REFRESH_PATH,
    )
    return response
