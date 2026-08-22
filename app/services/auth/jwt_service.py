"""Access-token and refresh-token business logic."""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import jwt

from app.core.auth_config import JWTSettings
from app.db.auth_repositories import AuthRepository, SubjectType
from app.db.models import AdminRole
from app.services.auth.exceptions import (
    AuthSubjectDisabledError,
    InvalidAccessTokenError,
    InvalidRefreshTokenError,
)

ACCESS_TOKEN_LIFETIME = timedelta(minutes=15)
REFRESH_TOKEN_LIFETIME = timedelta(days=30)


def _hash_refresh_token(raw_token: str) -> str:
    """Return the SHA-256 digest persisted for an opaque refresh token."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


class JWTService:
    """Issue and validate access tokens and rotate opaque refresh tokens."""

    def __init__(
        self,
        repository: AuthRepository,
        *,
        settings: JWTSettings | None = None,
    ) -> None:
        """Configure the service with injected persistence and signing settings."""
        self._repository = repository
        self._settings = settings or JWTSettings.from_environment()

    def issue_access_token(
        self,
        subject_id: UUID | str,
        subject_type: SubjectType,
        role: AdminRole | str | None = None,
    ) -> str:
        """Issue a signed access JWT with a 15-minute lifetime."""
        if subject_type == "admin" and role is None:
            raise ValueError("Admin access tokens require a role")
        if subject_type == "user" and role is not None:
            raise ValueError("User access tokens must not contain an admin role")
        if subject_type not in ("user", "admin"):
            raise ValueError(f"Unsupported subject_type: {subject_type!r}")

        now = datetime.now(tz=UTC)
        subject_id_text = str(subject_id)
        claims: dict[str, object] = {
            "sub": subject_id_text,
            "subject_id": subject_id_text,
            "subject_type": subject_type,
            "iat": now,
            "exp": now + ACCESS_TOKEN_LIFETIME,
        }
        if subject_type == "admin":
            claims["role"] = role.value if isinstance(role, AdminRole) else role
        return jwt.encode(
            claims,
            self._settings.secret,
            algorithm=self._settings.algorithm,
        )

    async def issue_refresh_token(
        self,
        subject_id: UUID,
        subject_type: SubjectType,
    ) -> str:
        """Persist a 30-day opaque refresh token hash and return its raw value once."""
        if subject_type not in ("user", "admin"):
            raise ValueError(f"Unsupported subject_type: {subject_type!r}")
        raw_token, token_hash, now = self._new_refresh_token()
        try:
            await self._repository.create_refresh_token(
                token_hash=token_hash,
                subject_id=subject_id,
                subject_type=subject_type,
                issued_at=now,
                expires_at=now + REFRESH_TOKEN_LIFETIME,
            )
            await self._repository.commit()
        except Exception:
            await self._repository.rollback()
            raise
        return raw_token

    def verify_access_token(self, token: str) -> dict[str, Any]:
        """Decode and structurally validate a signed access token."""
        try:
            claims = jwt.decode(
                token,
                self._settings.secret,
                algorithms=[self._settings.algorithm],
                options={
                    "require": ["sub", "subject_id", "subject_type", "iat", "exp"]
                },
            )
        except jwt.PyJWTError as error:
            raise InvalidAccessTokenError("Invalid access token") from error

        subject_type = claims.get("subject_type")
        subject_id = claims.get("subject_id")
        if subject_type not in ("user", "admin") or claims.get("sub") != subject_id:
            raise InvalidAccessTokenError("Invalid access token claims")
        if subject_type == "admin" and claims.get("role") not in {
            role.value for role in AdminRole
        }:
            raise InvalidAccessTokenError("Admin access token has an invalid role")
        if subject_type == "user" and "role" in claims:
            raise InvalidAccessTokenError("User access token must not contain a role")
        return claims

    async def rotate_refresh_token(
        self,
        raw_token: str,
        *,
        expected_subject_type: SubjectType | None = None,
    ) -> tuple[str, str]:
        """Atomically revoke a valid refresh token and issue its replacements."""
        now = datetime.now(tz=UTC)
        stored = await self._repository.get_refresh_token_for_update(
            _hash_refresh_token(raw_token)
        )
        if stored is None:
            await self._repository.rollback()
            raise InvalidRefreshTokenError("Invalid refresh token")
        if (
            expected_subject_type is not None
            and stored.subject_type != expected_subject_type
        ):
            await self._repository.rollback()
            raise InvalidRefreshTokenError("Invalid refresh token")

        if not stored.owner_is_enabled:
            await self._repository.revoke_all_refresh_tokens(
                subject_id=stored.subject_id,
                subject_type=stored.subject_type,
                revoked_at=now,
            )
            await self._repository.commit()
            raise AuthSubjectDisabledError("Refresh-token owner is disabled")

        if stored.revoked_at is not None or stored.expires_at <= now:
            await self._repository.rollback()
            raise InvalidRefreshTokenError("Invalid refresh token")

        new_raw_token, new_token_hash, issued_at = self._new_refresh_token()
        try:
            await self._repository.revoke_refresh_token(stored.id, now)
            await self._repository.create_refresh_token(
                token_hash=new_token_hash,
                subject_id=stored.subject_id,
                subject_type=stored.subject_type,
                issued_at=issued_at,
                expires_at=issued_at + REFRESH_TOKEN_LIFETIME,
            )
            await self._repository.commit()
        except Exception:
            await self._repository.rollback()
            raise

        access_token = self.issue_access_token(
            stored.subject_id,
            stored.subject_type,
            role=stored.role,
        )
        return access_token, new_raw_token

    async def revoke_refresh_token(
        self,
        raw_token: str,
        *,
        expected_subject_type: SubjectType | None = None,
    ) -> None:
        """Revoke a stored refresh token by its raw value."""
        stored = await self._repository.get_refresh_token_for_update(
            _hash_refresh_token(raw_token)
        )
        if stored is None:
            await self._repository.rollback()
            raise InvalidRefreshTokenError("Invalid refresh token")
        if (
            expected_subject_type is not None
            and stored.subject_type != expected_subject_type
        ):
            await self._repository.rollback()
            raise InvalidRefreshTokenError("Invalid refresh token")
        try:
            if stored.revoked_at is None:
                await self._repository.revoke_refresh_token(
                    stored.id,
                    datetime.now(tz=UTC),
                )
            await self._repository.commit()
        except Exception:
            await self._repository.rollback()
            raise

    @staticmethod
    def _new_refresh_token() -> tuple[str, str, datetime]:
        """Generate one high-entropy opaque token and its storage digest."""
        raw_token = secrets.token_urlsafe(48)
        return raw_token, _hash_refresh_token(raw_token), datetime.now(tz=UTC)
