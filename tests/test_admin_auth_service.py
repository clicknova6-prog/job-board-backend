"""Unit tests for administrator password authentication."""

from __future__ import annotations

import asyncio
from datetime import datetime
from uuid import UUID, uuid4

import pytest

from app.db.auth_repositories import AdminUserRecord
from app.db.models import AdminRole
from app.services.auth.admin_auth_service import (
    AdminAuthService,
    hash_password,
    verify_password,
)
from app.services.auth.exceptions import InvalidAdminCredentialsError


class _FakeAdminRepository:
    def __init__(self, admin: AdminUserRecord | None) -> None:
        self.admin = admin
        self.looked_up_email: str | None = None
        self.commits = 0
        self.rollbacks = 0

    async def get_admin_by_email(self, email: str) -> AdminUserRecord | None:
        self.looked_up_email = email
        if self.admin is not None and self.admin.email == email:
            return self.admin
        return None

    async def update_admin_last_login(
        self,
        admin_user_id: UUID,
        last_login_at: datetime,
    ) -> AdminUserRecord:
        assert self.admin is not None
        assert admin_user_id == self.admin.id
        self.admin = AdminUserRecord(
            id=self.admin.id,
            email=self.admin.email,
            password_hash=self.admin.password_hash,
            role=self.admin.role,
            is_active=self.admin.is_active,
            last_login_at=last_login_at,
        )
        return self.admin

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


def _admin(password: str, *, is_active: bool = True) -> AdminUserRecord:
    return AdminUserRecord(
        id=uuid4(),
        email="admin@example.com",
        password_hash=hash_password(password),
        role=AdminRole.ADMIN,
        is_active=is_active,
        last_login_at=None,
    )


def test_hash_and_verify_password() -> None:
    password_hash = hash_password("correct horse battery staple")
    assert password_hash.startswith("$argon2id$")
    assert verify_password("correct horse battery staple", password_hash)
    assert not verify_password("wrong password", password_hash)
    assert not verify_password("password", "not-an-argon2-hash")


def test_authenticate_admin_normalizes_email_and_updates_login() -> None:
    async def run() -> None:
        repository = _FakeAdminRepository(_admin("secret-password"))
        service = AdminAuthService(repository)  # type: ignore[arg-type]
        authenticated = await service.authenticate_admin(
            "  ADMIN@EXAMPLE.COM ",
            "secret-password",
        )
        assert repository.looked_up_email == "admin@example.com"
        assert authenticated.last_login_at is not None
        assert repository.commits == 1

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["missing", "inactive", "wrong-password"])
def test_authenticate_admin_uses_generic_failure(failure: str) -> None:
    async def run() -> None:
        if failure == "missing":
            admin = None
            password = "anything"
        elif failure == "inactive":
            admin = _admin("secret-password", is_active=False)
            password = "secret-password"
        else:
            admin = _admin("secret-password")
            password = "wrong-password"
        repository = _FakeAdminRepository(admin)
        service = AdminAuthService(repository)  # type: ignore[arg-type]

        with pytest.raises(
            InvalidAdminCredentialsError,
            match="^invalid credentials$",
        ):
            await service.authenticate_admin("admin@example.com", password)

    asyncio.run(run())
