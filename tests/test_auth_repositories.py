"""Unit tests for asynchronous authentication persistence operations."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.auth_repositories import (
    AdminUserRecord,
    AuthRepository,
    RefreshTokenRecord,
    UserRecord,
)
from app.db.models import AdminRole, AdminUser, OAuthProvider, RefreshToken, User

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


def _session() -> AsyncMock:
    return AsyncMock(spec=AsyncSession)


def _user(
    *,
    user_id: UUID | None = None,
    email: str = "user@example.test",
    provider: OAuthProvider = OAuthProvider.GOOGLE,
    subject_id: str = "google-subject-1",
    display_name: str | None = "Example User",
    deleted_at: datetime | None = None,
) -> User:
    return User(
        id=user_id or uuid4(),
        email=email,
        oauth_provider=provider,
        oauth_subject_id=subject_id,
        display_name=display_name,
        deleted_at=deleted_at,
    )


def _admin(
    *,
    admin_id: UUID | None = None,
    email: str = "admin@example.test",
    role: AdminRole = AdminRole.ADMIN,
    is_active: bool = True,
    last_login_at: datetime | None = None,
) -> AdminUser:
    return AdminUser(
        id=admin_id or uuid4(),
        email=email,
        password_hash="$argon2id$test-hash",
        role=role,
        is_active=is_active,
        last_login_at=last_login_at,
    )


def test_user_and_admin_lookups_return_immutable_records_or_none() -> None:
    session = _session()
    user = _user()
    admin = _admin(role=AdminRole.SUPER_ADMIN)
    session.scalar.side_effect = [user, None, user, None, admin, None]
    repository = AuthRepository(session)

    async def run() -> None:
        oauth_record = await repository.get_user_by_oauth_identity(
            OAuthProvider.GOOGLE,
            "google-subject-1",
        )
        missing_oauth = await repository.get_user_by_oauth_identity(
            OAuthProvider.APPLE,
            "missing",
        )
        email_record = await repository.get_user_by_email(user.email)
        missing_email = await repository.get_user_by_email("missing@example.test")
        admin_record = await repository.get_admin_by_email(admin.email)
        missing_admin = await repository.get_admin_by_email("missing-admin@example.test")

        assert oauth_record == UserRecord(
            id=user.id,
            email=user.email,
            oauth_provider=user.oauth_provider,
            oauth_subject_id=user.oauth_subject_id,
            display_name=user.display_name,
            deleted_at=None,
        )
        assert email_record == oauth_record
        assert admin_record == AdminUserRecord(
            id=admin.id,
            email=admin.email,
            password_hash=admin.password_hash,
            role=AdminRole.SUPER_ADMIN,
            is_active=True,
            last_login_at=None,
        )
        assert missing_oauth is None
        assert missing_email is None
        assert missing_admin is None

    asyncio.run(run())

    oauth_statement = session.scalar.await_args_list[0].args[0]
    email_statement = session.scalar.await_args_list[2].args[0]
    admin_statement = session.scalar.await_args_list[4].args[0]
    assert "users.oauth_provider" in str(oauth_statement)
    assert "users.oauth_subject_id" in str(oauth_statement)
    assert OAuthProvider.GOOGLE in oauth_statement.compile().params.values()
    assert "google-subject-1" in oauth_statement.compile().params.values()
    assert "users.email" in str(email_statement)
    assert user.email in email_statement.compile().params.values()
    assert "admin_users.email" in str(admin_statement)
    assert admin.email in admin_statement.compile().params.values()


def test_create_user_adds_and_flushes_without_committing() -> None:
    session = _session()
    generated_id = uuid4()

    async def assign_id() -> None:
        created_user = session.add.call_args.args[0]
        created_user.id = generated_id

    session.flush.side_effect = assign_id
    repository = AuthRepository(session)

    async def run() -> None:
        record = await repository.create_user(
            email="new@example.test",
            provider=OAuthProvider.APPLE,
            oauth_subject_id="apple-subject",
            display_name=None,
        )
        assert record == UserRecord(
            id=generated_id,
            email="new@example.test",
            oauth_provider=OAuthProvider.APPLE,
            oauth_subject_id="apple-subject",
            display_name=None,
            deleted_at=None,
        )

    asyncio.run(run())

    created_user = session.add.call_args.args[0]
    assert isinstance(created_user, User)
    assert created_user.email == "new@example.test"
    assert created_user.oauth_provider is OAuthProvider.APPLE
    assert created_user.oauth_subject_id == "apple-subject"
    session.flush.assert_awaited_once_with()
    session.commit.assert_not_awaited()


def test_create_user_duplicate_constraint_error_propagates() -> None:
    session = _session()
    duplicate_error = IntegrityError(
        "INSERT users",
        {},
        RuntimeError("users_email_unique_idx"),
    )
    session.flush.side_effect = duplicate_error

    async def run() -> None:
        with pytest.raises(IntegrityError) as exc_info:
            await AuthRepository(session).create_user(
                email="duplicate@example.test",
                provider=OAuthProvider.GOOGLE,
                oauth_subject_id="new-subject",
                display_name="Duplicate",
            )
        assert exc_info.value is duplicate_error

    asyncio.run(run())
    session.add.assert_called_once()
    session.commit.assert_not_awaited()
    session.rollback.assert_not_awaited()


def test_update_admin_last_login_flushes_and_returns_current_record() -> None:
    session = _session()
    admin = _admin(last_login_at=NOW - timedelta(days=1))
    session.get.return_value = admin
    repository = AuthRepository(session)

    async def run() -> None:
        record = await repository.update_admin_last_login(admin.id, NOW)
        assert record == AdminUserRecord(
            id=admin.id,
            email=admin.email,
            password_hash=admin.password_hash,
            role=admin.role,
            is_active=True,
            last_login_at=NOW,
        )

    asyncio.run(run())

    session.get.assert_awaited_once_with(AdminUser, admin.id)
    session.flush.assert_awaited_once_with()
    assert admin.last_login_at == NOW


def test_update_missing_admin_raises_without_flushing() -> None:
    session = _session()
    admin_id = uuid4()
    session.get.return_value = None

    async def run() -> None:
        with pytest.raises(LookupError, match=f"Admin user {admin_id} no longer exists"):
            await AuthRepository(session).update_admin_last_login(admin_id, NOW)

    asyncio.run(run())
    session.flush.assert_not_awaited()


@pytest.mark.parametrize("subject_type", ["user", "admin"])
def test_create_refresh_token_sets_exactly_one_owner_and_returns_id(
    subject_type: str,
) -> None:
    session = _session()
    subject_id = uuid4()
    token_id = uuid4()
    expires_at = NOW + timedelta(days=30)

    async def assign_id() -> None:
        token = session.add.call_args.args[0]
        token.id = token_id

    session.flush.side_effect = assign_id
    repository = AuthRepository(session)

    async def run() -> None:
        result = await repository.create_refresh_token(
            token_hash="hashed-refresh-token",
            subject_id=subject_id,
            subject_type=subject_type,  # type: ignore[arg-type]
            issued_at=NOW,
            expires_at=expires_at,
        )
        assert result == token_id

    asyncio.run(run())

    token = session.add.call_args.args[0]
    assert isinstance(token, RefreshToken)
    assert token.token_hash == "hashed-refresh-token"
    assert token.user_id == (subject_id if subject_type == "user" else None)
    assert token.admin_user_id == (subject_id if subject_type == "admin" else None)
    assert token.issued_at == NOW
    assert token.expires_at == expires_at
    session.flush.assert_awaited_once_with()
    session.commit.assert_not_awaited()


def test_refresh_token_lookup_returns_none_and_uses_row_lock() -> None:
    session = _session()
    result = Mock()
    result.one_or_none.return_value = None
    session.execute.return_value = result

    async def run() -> None:
        assert (
            await AuthRepository(session).get_refresh_token_for_update("missing-hash")
            is None
        )

    asyncio.run(run())

    statement = session.execute.await_args.args[0]
    sql = str(statement)
    assert "refresh_tokens.token_hash" in sql
    assert "LEFT OUTER JOIN users" in sql
    assert "LEFT OUTER JOIN admin_users" in sql
    assert "FOR UPDATE" in sql
    assert "missing-hash" in statement.compile().params.values()


@pytest.mark.parametrize(
    ("deleted_at", "owner_is_enabled"),
    [(None, True), (NOW, False)],
)
def test_refresh_token_lookup_maps_user_owner_state(
    deleted_at: datetime | None,
    owner_is_enabled: bool,
) -> None:
    session = _session()
    token_id = uuid4()
    user_id = uuid4()
    expires_at = NOW + timedelta(days=1)
    row = SimpleNamespace(
        id=token_id,
        user_id=user_id,
        admin_user_id=None,
        expires_at=expires_at,
        revoked_at=None,
        deleted_at=deleted_at,
        is_active=None,
        role=None,
    )
    result = Mock()
    result.one_or_none.return_value = row
    session.execute.return_value = result

    async def run() -> None:
        assert await AuthRepository(session).get_refresh_token_for_update(
            "user-token"
        ) == RefreshTokenRecord(
            id=token_id,
            subject_id=user_id,
            subject_type="user",
            expires_at=expires_at,
            revoked_at=None,
            role=None,
            owner_is_enabled=owner_is_enabled,
        )

    asyncio.run(run())


@pytest.mark.parametrize("is_active", [True, False])
def test_refresh_token_lookup_maps_admin_owner_state(is_active: bool) -> None:
    session = _session()
    token_id = uuid4()
    admin_id = uuid4()
    expires_at = NOW + timedelta(days=1)
    revoked_at = NOW - timedelta(minutes=5)
    row = SimpleNamespace(
        id=token_id,
        user_id=None,
        admin_user_id=admin_id,
        expires_at=expires_at,
        revoked_at=revoked_at,
        deleted_at=None,
        is_active=is_active,
        role=AdminRole.SUPER_ADMIN,
    )
    result = Mock()
    result.one_or_none.return_value = row
    session.execute.return_value = result

    async def run() -> None:
        assert await AuthRepository(session).get_refresh_token_for_update(
            "admin-token"
        ) == RefreshTokenRecord(
            id=token_id,
            subject_id=admin_id,
            subject_type="admin",
            expires_at=expires_at,
            revoked_at=revoked_at,
            role=AdminRole.SUPER_ADMIN,
            owner_is_enabled=is_active,
        )

    asyncio.run(run())


def test_refresh_token_without_owner_raises_database_invariant_error() -> None:
    session = _session()
    result = Mock()
    result.one_or_none.return_value = SimpleNamespace(
        id=uuid4(),
        user_id=None,
        admin_user_id=None,
        expires_at=NOW + timedelta(days=1),
        revoked_at=None,
        deleted_at=None,
        is_active=None,
        role=None,
    )
    session.execute.return_value = result

    async def run() -> None:
        with pytest.raises(RuntimeError, match="Refresh token has no owner"):
            await AuthRepository(session).get_refresh_token_for_update("invalid-token")

    asyncio.run(run())


def test_revoke_one_refresh_token_scopes_unrevoked_row() -> None:
    session = _session()
    token_id = uuid4()

    async def run() -> None:
        await AuthRepository(session).revoke_refresh_token(token_id, NOW)

    asyncio.run(run())

    statement = session.execute.await_args.args[0]
    sql = str(statement)
    parameters = statement.compile().params.values()
    assert sql.startswith("UPDATE refresh_tokens")
    assert "refresh_tokens.id" in sql
    assert "refresh_tokens.revoked_at IS NULL" in sql
    assert token_id in parameters
    assert NOW in parameters


@pytest.mark.parametrize(
    ("subject_type", "owner_column"),
    [("user", "refresh_tokens.user_id"), ("admin", "refresh_tokens.admin_user_id")],
)
def test_revoke_all_refresh_tokens_scopes_owner_type(
    subject_type: str,
    owner_column: str,
) -> None:
    session = _session()
    subject_id = uuid4()

    async def run() -> None:
        await AuthRepository(session).revoke_all_refresh_tokens(
            subject_id=subject_id,
            subject_type=subject_type,  # type: ignore[arg-type]
            revoked_at=NOW,
        )

    asyncio.run(run())

    statement = session.execute.await_args.args[0]
    sql = str(statement)
    parameters = statement.compile().params.values()
    assert owner_column in sql
    assert "refresh_tokens.revoked_at IS NULL" in sql
    assert subject_id in parameters
    assert NOW in parameters


def test_database_lookup_error_propagates() -> None:
    session = _session()
    database_error = OperationalError(
        "SELECT users",
        {},
        RuntimeError("database unavailable"),
    )
    session.scalar.side_effect = database_error

    async def run() -> None:
        with pytest.raises(OperationalError) as exc_info:
            await AuthRepository(session).get_user_by_email("user@example.test")
        assert exc_info.value is database_error

    asyncio.run(run())


def test_commit_and_rollback_delegate_to_async_session() -> None:
    session = _session()
    repository = AuthRepository(session)

    async def run() -> None:
        await repository.commit()
        await repository.rollback()

    asyncio.run(run())

    session.commit.assert_awaited_once_with()
    session.rollback.assert_awaited_once_with()
