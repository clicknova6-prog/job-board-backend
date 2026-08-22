"""Unit tests for access and refresh token behavior."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

import pytest

from app.core.auth_config import JWTSettings
from app.db.auth_repositories import RefreshTokenRecord, SubjectType
from app.db.models import AdminRole
from app.services.auth.exceptions import (
    AuthSubjectDisabledError,
    InvalidRefreshTokenError,
)
from app.services.auth.jwt_service import JWTService


@dataclass
class _StoredToken:
    id: UUID
    token_hash: str
    subject_id: UUID
    subject_type: SubjectType
    expires_at: datetime
    revoked_at: datetime | None = None


class _FakeTokenRepository:
    def __init__(
        self,
        *,
        owner_is_enabled: bool = True,
        admin_role: AdminRole | None = None,
    ) -> None:
        self.tokens: list[_StoredToken] = []
        self.owner_is_enabled = owner_is_enabled
        self.admin_role = admin_role
        self.commits = 0
        self.rollbacks = 0
        self.revoke_all_calls = 0

    async def create_refresh_token(
        self,
        *,
        token_hash: str,
        subject_id: UUID,
        subject_type: SubjectType,
        issued_at: datetime,
        expires_at: datetime,
    ) -> UUID:
        del issued_at
        token_id = uuid4()
        self.tokens.append(
            _StoredToken(
                id=token_id,
                token_hash=token_hash,
                subject_id=subject_id,
                subject_type=subject_type,
                expires_at=expires_at,
            )
        )
        return token_id

    async def get_refresh_token_for_update(
        self, token_hash: str
    ) -> RefreshTokenRecord | None:
        stored = next(
            (item for item in self.tokens if item.token_hash == token_hash),
            None,
        )
        if stored is None:
            return None
        return RefreshTokenRecord(
            id=stored.id,
            subject_id=stored.subject_id,
            subject_type=stored.subject_type,
            expires_at=stored.expires_at,
            revoked_at=stored.revoked_at,
            role=self.admin_role,
            owner_is_enabled=self.owner_is_enabled,
        )

    async def revoke_refresh_token(self, token_id: UUID, revoked_at: datetime) -> None:
        token = next(item for item in self.tokens if item.id == token_id)
        token.revoked_at = revoked_at

    async def revoke_all_refresh_tokens(
        self,
        *,
        subject_id: UUID,
        subject_type: SubjectType,
        revoked_at: datetime,
    ) -> None:
        self.revoke_all_calls += 1
        for token in self.tokens:
            if token.subject_id == subject_id and token.subject_type == subject_type:
                token.revoked_at = revoked_at

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


def _service(repository: _FakeTokenRepository) -> JWTService:
    return JWTService(
        repository,  # type: ignore[arg-type]
        settings=JWTSettings(
            secret="unit-test-secret-with-at-least-32-bytes",
            algorithm="HS256",
        ),
    )


def test_access_and_refresh_token_round_trip() -> None:
    async def run() -> None:
        subject_id = uuid4()
        repository = _FakeTokenRepository()
        service = _service(repository)

        initial_access = service.issue_access_token(subject_id, "user")
        initial_claims = service.verify_access_token(initial_access)
        assert initial_claims["subject_id"] == str(subject_id)
        assert initial_claims["subject_type"] == "user"
        assert "role" not in initial_claims

        raw_refresh = await service.issue_refresh_token(subject_id, "user")
        assert repository.tokens[0].token_hash != raw_refresh
        assert len(repository.tokens[0].token_hash) == 64

        access_token, rotated_raw = await service.rotate_refresh_token(raw_refresh)
        rotated_claims = service.verify_access_token(access_token)
        assert rotated_claims["subject_id"] == str(subject_id)
        assert rotated_raw != raw_refresh
        assert repository.tokens[0].revoked_at is not None
        assert repository.tokens[1].revoked_at is None

        await service.revoke_refresh_token(rotated_raw)
        assert repository.tokens[1].revoked_at is not None
        with pytest.raises(InvalidRefreshTokenError):
            await service.rotate_refresh_token(rotated_raw)

    asyncio.run(run())


@pytest.mark.parametrize(
    ("subject_type", "role"),
    [("user", None), ("admin", AdminRole.ADMIN)],
)
def test_disabled_owner_rejects_rotation_and_revokes_all_tokens(
    subject_type: SubjectType,
    role: AdminRole | None,
) -> None:
    async def run() -> None:
        subject_id = uuid4()
        repository = _FakeTokenRepository(
            owner_is_enabled=False,
            admin_role=role,
        )
        service = _service(repository)
        first = await service.issue_refresh_token(subject_id, subject_type)
        await service.issue_refresh_token(subject_id, subject_type)

        with pytest.raises(AuthSubjectDisabledError):
            await service.rotate_refresh_token(first)

        assert repository.revoke_all_calls == 1
        assert all(token.revoked_at is not None for token in repository.tokens)

    asyncio.run(run())


def test_refresh_token_scope_mismatch_is_rejected_before_mutation() -> None:
    async def run() -> None:
        subject_id = uuid4()
        repository = _FakeTokenRepository()
        service = _service(repository)
        raw_token = await service.issue_refresh_token(subject_id, "user")

        with pytest.raises(InvalidRefreshTokenError):
            await service.rotate_refresh_token(
                raw_token,
                expected_subject_type="admin",
            )

        assert repository.tokens[0].revoked_at is None
        assert len(repository.tokens) == 1

    asyncio.run(run())
