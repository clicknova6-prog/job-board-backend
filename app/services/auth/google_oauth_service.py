"""Google OAuth exchange and job-seeker account provisioning."""

from __future__ import annotations

from typing import Any

import httpx
from sqlalchemy.exc import IntegrityError

from app.core.auth_config import GoogleOAuthSettings
from app.db.auth_repositories import AuthRepository, UserRecord
from app.db.models import OAuthProvider
from app.services.auth.exceptions import (
    GoogleOAuthExchangeError,
    OAuthEmailCollisionError,
)

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"


class GoogleOAuthService:
    """Exchange Google authorization codes and provision local users."""

    def __init__(
        self,
        repository: AuthRepository,
        *,
        settings: GoogleOAuthSettings | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        """Configure injected persistence, OAuth settings, and optional HTTP client."""
        self._repository = repository
        self._settings = settings or GoogleOAuthSettings.from_environment()
        self._http_client = http_client

    async def exchange_code_for_profile(
        self, authorization_code: str
    ) -> tuple[str, str, str | None]:
        """Exchange a Google code and return email, subject ID, and display name."""
        if not authorization_code:
            raise GoogleOAuthExchangeError("Google authorization code is required")

        if self._http_client is not None:
            return await self._exchange(self._http_client, authorization_code)
        async with httpx.AsyncClient(timeout=10.0) as client:
            return await self._exchange(client, authorization_code)

    async def get_or_create_user(
        self,
        email: str,
        oauth_subject_id: str,
        display_name: str | None,
    ) -> UserRecord:
        """Resolve Google identity first, then create without merging by email."""
        normalized_email = email.strip().lower()
        if not normalized_email:
            raise ValueError("Google profile email is required")
        if not oauth_subject_id:
            raise ValueError("Google OAuth subject ID is required")

        existing_identity = await self._repository.get_user_by_oauth_identity(
            OAuthProvider.GOOGLE,
            oauth_subject_id,
        )
        if existing_identity is not None:
            return existing_identity

        existing_email = await self._repository.get_user_by_email(normalized_email)
        if existing_email is not None:
            raise OAuthEmailCollisionError(
                "Email is already linked to a different OAuth identity; "
                "account linking policy is required"
            )

        try:
            user = await self._repository.create_user(
                email=normalized_email,
                provider=OAuthProvider.GOOGLE,
                oauth_subject_id=oauth_subject_id,
                display_name=display_name,
            )
            await self._repository.commit()
            return user
        except IntegrityError:
            await self._repository.rollback()
            concurrent_identity = await self._repository.get_user_by_oauth_identity(
                OAuthProvider.GOOGLE,
                oauth_subject_id,
            )
            if concurrent_identity is not None:
                return concurrent_identity
            concurrent_email = await self._repository.get_user_by_email(
                normalized_email
            )
            if concurrent_email is not None:
                raise OAuthEmailCollisionError(
                    "Email is already linked to a different OAuth identity; "
                    "account linking policy is required"
                )
            raise
        except Exception:
            await self._repository.rollback()
            raise

    async def _exchange(
        self,
        client: httpx.AsyncClient,
        authorization_code: str,
    ) -> tuple[str, str, str | None]:
        """Perform the token and userinfo HTTP requests with one client."""
        try:
            token_response = await client.post(
                GOOGLE_TOKEN_URL,
                data={
                    "code": authorization_code,
                    "client_id": self._settings.client_id,
                    "client_secret": self._settings.client_secret,
                    "redirect_uri": self._settings.redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
            token_response.raise_for_status()
            access_token = token_response.json().get("access_token")
            if not isinstance(access_token, str) or not access_token:
                raise ValueError("Google token response omitted access_token")

            profile_response = await client.get(
                GOOGLE_USERINFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            profile_response.raise_for_status()
            profile: dict[str, Any] = profile_response.json()
            email = profile.get("email")
            subject_id = profile.get("sub")
            display_name = profile.get("name")
            if not isinstance(email, str) or not email:
                raise ValueError("Google userinfo omitted email")
            if not isinstance(subject_id, str) or not subject_id:
                raise ValueError("Google userinfo omitted subject ID")
            if display_name is not None and not isinstance(display_name, str):
                raise ValueError("Google userinfo returned an invalid display name")
            return email, subject_id, display_name
        except (httpx.HTTPError, ValueError) as error:
            raise GoogleOAuthExchangeError("Google OAuth exchange failed") from error
