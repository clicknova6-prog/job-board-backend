"""Administrator password authentication business logic."""

from __future__ import annotations

from datetime import UTC, datetime

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.db.auth_repositories import AdminUserRecord, AuthRepository
from app.services.auth.exceptions import InvalidAdminCredentialsError

_PASSWORD_HASHER = PasswordHasher()


def hash_password(plain: str) -> str:
    """Hash an administrator password with Argon2id."""
    if not plain:
        raise ValueError("Password must not be empty")
    return _PASSWORD_HASHER.hash(plain)


def verify_password(plain: str, password_hash: str) -> bool:
    """Return whether a plaintext password matches an Argon2 hash."""
    try:
        return _PASSWORD_HASHER.verify(password_hash, plain)
    except (InvalidHashError, VerificationError, VerifyMismatchError):
        return False


class AdminAuthService:
    """Authenticate active administrators without exposing failure details."""

    def __init__(self, repository: AuthRepository) -> None:
        """Configure the service with injected async persistence."""
        self._repository = repository

    async def authenticate_admin(
        self,
        email: str,
        password: str,
    ) -> AdminUserRecord:
        """Verify credentials, record login time, and return the administrator."""
        normalized_email = email.strip().lower()
        admin = await self._repository.get_admin_by_email(normalized_email)
        if (
            admin is None
            or not admin.is_active
            or not verify_password(password, admin.password_hash)
        ):
            await self._repository.rollback()
            raise InvalidAdminCredentialsError("invalid credentials")

        try:
            authenticated = await self._repository.update_admin_last_login(
                admin.id,
                datetime.now(tz=UTC),
            )
            await self._repository.commit()
            return authenticated
        except Exception:
            await self._repository.rollback()
            raise
