"""ASGI integration tests for public and administrator auth routes."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Annotated
from urllib.parse import parse_qs, urlparse
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import Depends, FastAPI

from app.auth.cookies import (
    ADMIN_REFRESH_COOKIE,
    PUBLIC_REFRESH_COOKIE,
)
from app.auth.dependencies import (
    get_admin_auth_service,
    get_current_admin,
    get_current_user,
    get_google_oauth_service,
    get_google_oauth_settings,
    get_jwt_service,
    require_admin_role,
)
from app.auth.schemas import CurrentAdmin, CurrentUser
from app.core.auth_config import GoogleOAuthSettings, JWTSettings
from app.core.rate_limit import limiter
from app.db.auth_repositories import AdminUserRecord, UserRecord
from app.db.models import AdminRole, OAuthProvider
from app.main import app
from app.services.auth.exceptions import (
    GoogleOAuthExchangeError,
    InvalidAdminCredentialsError,
    OAuthEmailCollisionError,
)
from app.services.auth.google_oauth_service import (
    GOOGLE_TOKEN_URL,
    GOOGLE_USERINFO_URL,
    GoogleOAuthService,
)
from app.services.auth.jwt_service import JWTService


@pytest.fixture(autouse=True)
def _reset_app_state() -> Iterator[None]:
    app.dependency_overrides.clear()
    limiter.reset()
    yield
    app.dependency_overrides.clear()
    limiter.reset()


def _google_settings() -> GoogleOAuthSettings:
    return GoogleOAuthSettings(
        client_id="route-client-id",
        client_secret="route-client-secret",
        redirect_uri="https://testserver/auth/google/callback",
    )


class _RouteJWTService:
    def __init__(self) -> None:
        self.rotations: list[tuple[str, str | None]] = []
        self.revocations: list[tuple[str, str | None]] = []

    def issue_access_token(
        self,
        subject_id: UUID,
        subject_type: str,
        role: AdminRole | None = None,
    ) -> str:
        role_value = role.value if role is not None else "none"
        return f"access:{subject_type}:{subject_id}:{role_value}"

    async def issue_refresh_token(self, subject_id: UUID, subject_type: str) -> str:
        return f"refresh:{subject_type}:{subject_id}"

    async def rotate_refresh_token(
        self,
        raw_token: str,
        *,
        expected_subject_type: str | None = None,
    ) -> tuple[str, str]:
        self.rotations.append((raw_token, expected_subject_type))
        return (
            f"rotated-access:{expected_subject_type}",
            f"rotated-refresh:{expected_subject_type}",
        )

    async def revoke_refresh_token(
        self,
        raw_token: str,
        *,
        expected_subject_type: str | None = None,
    ) -> None:
        self.revocations.append((raw_token, expected_subject_type))


class _GoogleRepository:
    def __init__(self) -> None:
        self.user: UserRecord | None = None

    async def get_user_by_oauth_identity(
        self,
        provider: OAuthProvider,
        oauth_subject_id: str,
    ) -> UserRecord | None:
        if (
            self.user is not None
            and self.user.oauth_provider == provider
            and self.user.oauth_subject_id == oauth_subject_id
        ):
            return self.user
        return None

    async def get_user_by_email(self, email: str) -> UserRecord | None:
        if self.user is not None and self.user.email == email:
            return self.user
        return None

    async def create_user(
        self,
        *,
        email: str,
        provider: OAuthProvider,
        oauth_subject_id: str,
        display_name: str | None,
    ) -> UserRecord:
        self.user = UserRecord(
            id=uuid4(),
            email=email,
            oauth_provider=provider,
            oauth_subject_id=oauth_subject_id,
            display_name=display_name,
            deleted_at=None,
        )
        return self.user

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


class _CollisionGoogleService:
    async def exchange_code_for_profile(
        self, authorization_code: str
    ) -> tuple[str, str, str | None]:
        del authorization_code
        return "collision@example.com", "new-subject", "Collision"

    async def get_or_create_user(
        self,
        email: str,
        oauth_subject_id: str,
        display_name: str | None,
    ) -> UserRecord:
        del email, oauth_subject_id, display_name
        raise OAuthEmailCollisionError("internal collision details")


class _FailingGoogleService:
    async def exchange_code_for_profile(
        self, authorization_code: str
    ) -> tuple[str, str, str | None]:
        del authorization_code
        raise GoogleOAuthExchangeError("internal provider details")


class _RouteAdminService:
    def __init__(self, *, valid: bool = True) -> None:
        self.valid = valid
        self.admin = AdminUserRecord(
            id=uuid4(),
            email="admin@example.com",
            password_hash="unused",
            role=AdminRole.SUPER_ADMIN,
            is_active=True,
            last_login_at=None,
        )

    async def authenticate_admin(
        self,
        email: str,
        password: str,
    ) -> AdminUserRecord:
        if not self.valid:
            raise InvalidAdminCredentialsError("invalid credentials")
        assert email == "ADMIN@example.com"
        assert password == "password"
        return self.admin


def _asgi_client(*, client_host: str = "testclient") -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, client=(client_host, 123))
    return httpx.AsyncClient(
        transport=transport,
        base_url="https://testserver",
        follow_redirects=False,
    )


def test_google_login_redirect_and_rate_limit() -> None:
    async def run() -> None:
        app.dependency_overrides[get_google_oauth_settings] = _google_settings
        async with _asgi_client(client_host="google-login-rate") as client:
            responses = [await client.get("/auth/google/login") for _ in range(6)]

        assert [response.status_code for response in responses] == [
            302,
            302,
            302,
            302,
            302,
            429,
        ]
        first = responses[0]
        query = parse_qs(urlparse(first.headers["location"]).query)
        assert query["client_id"] == [_google_settings().client_id]
        assert query["redirect_uri"] == [_google_settings().redirect_uri]
        assert query["scope"] == ["openid email profile"]
        assert query["state"]
        cookie_header = first.headers["set-cookie"].lower()
        assert "httponly" in cookie_header
        assert "secure" in cookie_header
        assert "samesite=lax" in cookie_header

    asyncio.run(run())


def test_google_callback_uses_mocked_http_and_sets_refresh_cookie() -> None:
    async def run() -> None:
        repository = _GoogleRepository()
        jwt_service = _RouteJWTService()

        def google_handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == GOOGLE_TOKEN_URL:
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

        google_http = httpx.AsyncClient(transport=httpx.MockTransport(google_handler))
        google_service = GoogleOAuthService(
            repository,  # type: ignore[arg-type]
            settings=_google_settings(),
            http_client=google_http,
        )
        app.dependency_overrides[get_google_oauth_settings] = _google_settings
        app.dependency_overrides[get_google_oauth_service] = lambda: google_service
        app.dependency_overrides[get_jwt_service] = lambda: jwt_service

        async with _asgi_client(client_host="google-callback") as client:
            login = await client.get("/auth/google/login")
            state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
            callback = await client.get(
                "/auth/google/callback",
                params={"code": "authorization-code", "state": state},
            )
        await google_http.aclose()

        assert callback.status_code == 200
        assert callback.json()["access_token"].startswith("access:user:")
        assert repository.user is not None
        assert repository.user.email == "person@example.com"
        cookie_header = callback.headers["set-cookie"].lower()
        assert PUBLIC_REFRESH_COOKIE in cookie_header
        assert "httponly" in cookie_header
        assert "secure" in cookie_header
        assert "samesite=lax" in cookie_header

    asyncio.run(run())


@pytest.mark.parametrize(
    ("params", "expected_detail"),
    [
        ({"code": "code", "state": "wrong-state"}, "Invalid Google OAuth callback"),
        ({"error": "access_denied"}, "Google authorization was not completed"),
    ],
)
def test_google_callback_handles_state_and_provider_errors(
    params: dict[str, str],
    expected_detail: str,
) -> None:
    async def run() -> None:
        app.dependency_overrides[get_google_oauth_service] = _CollisionGoogleService
        app.dependency_overrides[get_jwt_service] = _RouteJWTService
        async with _asgi_client(
            client_host=f"callback-error-{expected_detail}"
        ) as client:
            response = await client.get("/auth/google/callback", params=params)
        assert response.status_code == 400
        assert response.json() == {"detail": expected_detail}

    asyncio.run(run())


def test_google_email_collision_returns_non_leaking_conflict() -> None:
    async def run() -> None:
        app.dependency_overrides[get_google_oauth_service] = _CollisionGoogleService
        app.dependency_overrides[get_jwt_service] = _RouteJWTService
        async with _asgi_client(client_host="collision") as client:
            client.cookies.set(
                "job_board_google_oauth_state",
                "valid-state",
                path="/auth/google",
            )
            response = await client.get(
                "/auth/google/callback",
                params={"code": "code", "state": "valid-state"},
            )
        assert response.status_code == 409
        assert response.json() == {"detail": "An account already exists for this email"}
        assert "internal" not in response.text

    asyncio.run(run())


def test_google_exchange_failure_returns_non_leaking_bad_gateway() -> None:
    async def run() -> None:
        app.dependency_overrides[get_google_oauth_service] = _FailingGoogleService
        app.dependency_overrides[get_jwt_service] = _RouteJWTService
        async with _asgi_client(client_host="google-exchange-failure") as client:
            client.cookies.set(
                "job_board_google_oauth_state",
                "valid-state",
                path="/auth/google",
            )
            response = await client.get(
                "/auth/google/callback",
                params={"code": "code", "state": "valid-state"},
            )
        assert response.status_code == 502
        assert response.json() == {"detail": "Google authentication failed"}
        assert "internal" not in response.text

    asyncio.run(run())


def test_public_refresh_logout_and_refresh_rate_limit() -> None:
    async def run() -> None:
        jwt_service = _RouteJWTService()
        app.dependency_overrides[get_jwt_service] = lambda: jwt_service
        headers = {"Cookie": f"{PUBLIC_REFRESH_COOKIE}=public-raw-token"}
        async with _asgi_client(client_host="public-refresh") as client:
            responses = [
                await client.post("/auth/refresh", headers=headers) for _ in range(31)
            ]
            logout = await client.post("/auth/logout", headers=headers)

        assert all(response.status_code == 200 for response in responses[:30])
        assert responses[30].status_code == 429
        assert jwt_service.rotations[0] == ("public-raw-token", "user")
        assert logout.status_code == 204
        assert jwt_service.revocations[-1] == ("public-raw-token", "user")
        assert "max-age=0" in logout.headers["set-cookie"].lower()

    asyncio.run(run())


def test_admin_login_refresh_and_logout() -> None:
    async def run() -> None:
        jwt_service = _RouteJWTService()
        admin_service = _RouteAdminService()
        app.dependency_overrides[get_jwt_service] = lambda: jwt_service
        app.dependency_overrides[get_admin_auth_service] = lambda: admin_service
        async with _asgi_client(client_host="admin-flow") as client:
            login = await client.post(
                "/admin/auth/login",
                json={"email": "ADMIN@example.com", "password": "password"},
            )
            refresh = await client.post("/admin/auth/refresh")
            logout = await client.post("/admin/auth/logout")

        assert login.status_code == 200
        assert ":super_admin" in login.json()["access_token"]
        assert ADMIN_REFRESH_COOKIE in login.headers["set-cookie"].lower()
        assert "httponly" in login.headers["set-cookie"].lower()
        assert "secure" in login.headers["set-cookie"].lower()
        assert "samesite=lax" in login.headers["set-cookie"].lower()
        assert refresh.status_code == 200
        assert jwt_service.rotations == [
            (f"refresh:admin:{admin_service.admin.id}", "admin")
        ]
        assert logout.status_code == 204
        assert jwt_service.revocations == [("rotated-refresh:admin", "admin")]

    asyncio.run(run())


def test_admin_login_failure_is_generic() -> None:
    async def run() -> None:
        app.dependency_overrides[get_admin_auth_service] = lambda: _RouteAdminService(
            valid=False
        )
        app.dependency_overrides[get_jwt_service] = _RouteJWTService
        async with _asgi_client(client_host="admin-invalid") as client:
            response = await client.post(
                "/admin/auth/login",
                json={"email": "unknown@example.com", "password": "wrong"},
            )
        assert response.status_code == 401
        assert response.json() == {"detail": "invalid credentials"}

    asyncio.run(run())


def test_current_user_and_admin_dependencies_enforce_token_scope_and_role() -> None:
    async def run() -> None:
        protected_app = FastAPI()
        super_admin_dependency = require_admin_role(AdminRole.SUPER_ADMIN)
        super_admin_depends = Depends(super_admin_dependency)

        @protected_app.get("/user")
        async def user_endpoint(
            user: Annotated[CurrentUser, Depends(get_current_user)],
        ) -> dict[str, str]:
            return {"id": str(user.id)}

        @protected_app.get("/admin")
        async def admin_endpoint(
            admin: Annotated[CurrentAdmin, Depends(get_current_admin)],
        ) -> dict[str, str]:
            return {"id": str(admin.id), "role": admin.role.value}

        @protected_app.get("/super-admin")
        async def super_admin_endpoint(
            admin: CurrentAdmin = super_admin_depends,
        ) -> dict[str, str]:
            return {"id": str(admin.id)}

        jwt_service = JWTService(
            object(),  # type: ignore[arg-type]
            settings=JWTSettings(
                secret="route-test-secret-with-at-least-32-bytes",
                algorithm="HS256",
            ),
        )
        protected_app.dependency_overrides[get_jwt_service] = lambda: jwt_service
        user_id = uuid4()
        admin_id = uuid4()
        user_token = jwt_service.issue_access_token(user_id, "user")
        admin_token = jwt_service.issue_access_token(
            admin_id,
            "admin",
            role=AdminRole.ADMIN,
        )
        transport = httpx.ASGITransport(app=protected_app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="https://testserver",
        ) as client:
            user_response = await client.get(
                "/user", headers={"Authorization": f"Bearer {user_token}"}
            )
            wrong_scope = await client.get(
                "/admin", headers={"Authorization": f"Bearer {user_token}"}
            )
            admin_response = await client.get(
                "/admin", headers={"Authorization": f"Bearer {admin_token}"}
            )
            wrong_role = await client.get(
                "/super-admin",
                headers={"Authorization": f"Bearer {admin_token}"},
            )

        assert user_response.status_code == 200
        assert user_response.json() == {"id": str(user_id)}
        assert wrong_scope.status_code == 401
        assert admin_response.status_code == 200
        assert admin_response.json() == {
            "id": str(admin_id),
            "role": "admin",
        }
        assert wrong_role.status_code == 403

    asyncio.run(run())
