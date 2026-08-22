"""Async persistence operations for authentication services."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AdminRole, AdminUser, OAuthProvider, RefreshToken, User

SubjectType = Literal["user", "admin"]


@dataclass(frozen=True, slots=True)
class UserRecord:
    """Job-seeker account data exposed outside the repository layer."""

    id: UUID
    email: str
    oauth_provider: OAuthProvider
    oauth_subject_id: str
    display_name: str | None
    deleted_at: datetime | None


@dataclass(frozen=True, slots=True)
class AdminUserRecord:
    """Administrator account data exposed outside the repository layer."""

    id: UUID
    email: str
    password_hash: str
    role: AdminRole
    is_active: bool
    last_login_at: datetime | None


@dataclass(frozen=True, slots=True)
class RefreshTokenRecord:
    """Refresh-token and owner state used during rotation."""

    id: UUID
    subject_id: UUID
    subject_type: SubjectType
    expires_at: datetime
    revoked_at: datetime | None
    role: AdminRole | None
    owner_is_enabled: bool


def _user_record(user: User) -> UserRecord:
    """Copy a User ORM object into an immutable service-layer record."""
    return UserRecord(
        id=user.id,
        email=user.email,
        oauth_provider=user.oauth_provider,
        oauth_subject_id=user.oauth_subject_id,
        display_name=user.display_name,
        deleted_at=user.deleted_at,
    )


def _admin_record(admin: AdminUser) -> AdminUserRecord:
    """Copy an AdminUser ORM object into an immutable service-layer record."""
    return AdminUserRecord(
        id=admin.id,
        email=admin.email,
        password_hash=admin.password_hash,
        role=admin.role,
        is_active=admin.is_active,
        last_login_at=admin.last_login_at,
    )


class AuthRepository:
    """All ORM access required by the authentication service layer."""

    def __init__(self, session: AsyncSession) -> None:
        """Store the async SQLAlchemy session."""
        self._session = session

    async def get_user_by_oauth_identity(
        self,
        provider: OAuthProvider,
        oauth_subject_id: str,
    ) -> UserRecord | None:
        """Find a job seeker by stable provider identity."""
        user = await self._session.scalar(
            select(User).where(
                User.oauth_provider == provider,
                User.oauth_subject_id == oauth_subject_id,
            )
        )
        return _user_record(user) if user is not None else None

    async def get_user_by_email(self, email: str) -> UserRecord | None:
        """Find a job seeker by its normalized email."""
        user = await self._session.scalar(select(User).where(User.email == email))
        return _user_record(user) if user is not None else None

    async def create_user(
        self,
        *,
        email: str,
        provider: OAuthProvider,
        oauth_subject_id: str,
        display_name: str | None,
    ) -> UserRecord:
        """Create and flush a job-seeker account without committing."""
        user = User(
            email=email,
            oauth_provider=provider,
            oauth_subject_id=oauth_subject_id,
            display_name=display_name,
        )
        self._session.add(user)
        await self._session.flush()
        return _user_record(user)

    async def get_admin_by_email(self, email: str) -> AdminUserRecord | None:
        """Find an administrator by its normalized email."""
        admin = await self._session.scalar(
            select(AdminUser).where(AdminUser.email == email)
        )
        return _admin_record(admin) if admin is not None else None

    async def update_admin_last_login(
        self,
        admin_user_id: UUID,
        last_login_at: datetime,
    ) -> AdminUserRecord:
        """Update an administrator login timestamp and return current account data."""
        admin = await self._session.get(AdminUser, admin_user_id)
        if admin is None:
            raise LookupError(f"Admin user {admin_user_id} no longer exists")
        admin.last_login_at = last_login_at
        await self._session.flush()
        return _admin_record(admin)

    async def create_refresh_token(
        self,
        *,
        token_hash: str,
        subject_id: UUID,
        subject_type: SubjectType,
        issued_at: datetime,
        expires_at: datetime,
    ) -> UUID:
        """Create and flush a hashed refresh-token row without committing."""
        token = RefreshToken(
            token_hash=token_hash,
            user_id=subject_id if subject_type == "user" else None,
            admin_user_id=subject_id if subject_type == "admin" else None,
            issued_at=issued_at,
            expires_at=expires_at,
        )
        self._session.add(token)
        await self._session.flush()
        return token.id

    async def get_refresh_token_for_update(
        self, token_hash: str
    ) -> RefreshTokenRecord | None:
        """Lock a refresh token and load the status of its owning account."""
        row = (
            await self._session.execute(
                select(
                    RefreshToken.id,
                    RefreshToken.user_id,
                    RefreshToken.admin_user_id,
                    RefreshToken.expires_at,
                    RefreshToken.revoked_at,
                    User.deleted_at,
                    AdminUser.is_active,
                    AdminUser.role,
                )
                .outerjoin(User, RefreshToken.user_id == User.id)
                .outerjoin(AdminUser, RefreshToken.admin_user_id == AdminUser.id)
                .where(RefreshToken.token_hash == token_hash)
                .with_for_update(of=RefreshToken)
            )
        ).one_or_none()
        if row is None:
            return None
        if row.user_id is not None:
            return RefreshTokenRecord(
                id=row.id,
                subject_id=row.user_id,
                subject_type="user",
                expires_at=row.expires_at,
                revoked_at=row.revoked_at,
                role=None,
                owner_is_enabled=row.deleted_at is None,
            )
        if row.admin_user_id is None:
            raise RuntimeError(
                "Refresh token has no owner despite its database constraint"
            )
        return RefreshTokenRecord(
            id=row.id,
            subject_id=row.admin_user_id,
            subject_type="admin",
            expires_at=row.expires_at,
            revoked_at=row.revoked_at,
            role=row.role,
            owner_is_enabled=bool(row.is_active),
        )

    async def revoke_refresh_token(self, token_id: UUID, revoked_at: datetime) -> None:
        """Mark one refresh token revoked if it is not already revoked."""
        await self._session.execute(
            update(RefreshToken)
            .where(
                RefreshToken.id == token_id,
                RefreshToken.revoked_at.is_(None),
            )
            .values(revoked_at=revoked_at)
        )

    async def revoke_all_refresh_tokens(
        self,
        *,
        subject_id: UUID,
        subject_type: SubjectType,
        revoked_at: datetime,
    ) -> None:
        """Revoke every outstanding token belonging to one account."""
        owner_column = (
            RefreshToken.user_id
            if subject_type == "user"
            else RefreshToken.admin_user_id
        )
        await self._session.execute(
            update(RefreshToken)
            .where(
                owner_column == subject_id,
                RefreshToken.revoked_at.is_(None),
            )
            .values(revoked_at=revoked_at)
        )

    async def commit(self) -> None:
        """Commit the active authentication transaction."""
        await self._session.commit()

    async def rollback(self) -> None:
        """Roll back the active authentication transaction."""
        await self._session.rollback()
