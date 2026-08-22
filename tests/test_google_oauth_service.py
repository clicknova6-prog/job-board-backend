"""Unit tests for Google OAuth exchange and user provisioning."""

from __future__ import annotations

import asyncio
from urllib.parse import parse_qs
from uuid import uuid4

import httpx
import pytest

from app.core.auth_config import GoogleOAuthSettings
from app.db.auth_repositories import UserRecord
from app.db.models import OAuthProvider
from app.services.auth.exceptions import OAuthEmailCollisionError
from app.services.auth.google_oauth_service import (
    GOOGLE_TOKEN_URL,
    GOOGLE_USERINFO_URL,
    GoogleOAuthService,
)


class _FakeUserRepository:
    def __init__(self, users: list[UserRecord] | None = None) -> None:
        self.users = users or []
        self.created = 0
        self.commits = 0
        self.rollbacks = 0

    async def get_user_by_oauth_identity(
        self,
        provider: OAuthProvider,
        oauth_subject_id: str,
    ) -> UserRecord | None:
        return next(
            (
                user
                for user in self.users
                if user.oauth_provider == provider
                and user.oauth_subject_id == oauth_subject_id
            ),
            None,
        )

    async def get_user_by_email(self, email: str) -> UserRecord | None:
        return next((user for user in self.users if user.email == email), None)

    async def create_user(
        self,
        *,
        email: str,
        provider: OAuthProvider,
        oauth_subject_id: str,
        display_name: str | None,
    ) -> UserRecord:
        self.created += 1
        user = UserRecord(
            id=uuid4(),
            email=email,
            oauth_provider=provider,
            oauth_subject_id=oauth_subject_id,
            display_name=display_name,
            deleted_at=None,
        )
        self.users.append(user)
        return user

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


def _settings() -> GoogleOAuthSettings:
    return GoogleOAuthSettings(
        client_id="google-client-id",
        client_secret="google-client-secret",
        redirect_uri="https://example.com/auth/google/callback",
    )


def _user(email: str, subject_id: str) -> UserRecord:
    return UserRecord(
        id=uuid4(),
        email=email,
        oauth_provider=OAuthProvider.GOOGLE,
        oauth_subject_id=subject_id,
        display_name="Existing User",
        deleted_at=None,
    )


def test_exchange_code_for_profile_uses_google_endpoints() -> None:
    async def run() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == GOOGLE_TOKEN_URL:
                form = parse_qs(request.content.decode("utf-8"))
                assert form["code"] == ["authorization-code"]
                assert form["redirect_uri"] == [_settings().redirect_uri]
                return httpx.Response(200, json={"access_token": "google-access"})
            assert str(request.url) == GOOGLE_USERINFO_URL
            assert request.headers["Authorization"] == "Bearer google-access"
            return httpx.Response(
                200,
                json={
                    "email": "Person@Example.com",
                    "sub": "google-subject",
                    "name": "Person",
                },
            )

        repository = _FakeUserRepository()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            service = GoogleOAuthService(
                repository,  # type: ignore[arg-type]
                settings=_settings(),
                http_client=client,
            )
            profile = await service.exchange_code_for_profile("authorization-code")
        assert profile == ("Person@Example.com", "google-subject", "Person")

    asyncio.run(run())


def test_get_or_create_user_creates_normalized_user() -> None:
    async def run() -> None:
        repository = _FakeUserRepository()
        service = GoogleOAuthService(
            repository,  # type: ignore[arg-type]
            settings=_settings(),
        )
        user = await service.get_or_create_user(
            "  Person@Example.COM ",
            "new-subject",
            "Person",
        )
        assert user.email == "person@example.com"
        assert user.oauth_subject_id == "new-subject"
        assert repository.created == 1
        assert repository.commits == 1

    asyncio.run(run())


def test_get_or_create_user_returns_stable_existing_identity_first() -> None:
    async def run() -> None:
        existing = _user("original@example.com", "stable-subject")
        repository = _FakeUserRepository([existing])
        service = GoogleOAuthService(
            repository,  # type: ignore[arg-type]
            settings=_settings(),
        )
        resolved = await service.get_or_create_user(
            "changed@example.com",
            "stable-subject",
            "Changed Name",
        )
        assert resolved is existing
        assert repository.created == 0

    asyncio.run(run())


def test_get_or_create_user_rejects_email_collision() -> None:
    async def run() -> None:
        repository = _FakeUserRepository(
            [_user("person@example.com", "different-subject")]
        )
        service = GoogleOAuthService(
            repository,  # type: ignore[arg-type]
            settings=_settings(),
        )
        with pytest.raises(OAuthEmailCollisionError, match="account linking policy"):
            await service.get_or_create_user(
                "PERSON@EXAMPLE.COM",
                "new-subject",
                "Person",
            )
        assert repository.created == 0

    asyncio.run(run())
