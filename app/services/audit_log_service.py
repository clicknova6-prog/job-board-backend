"""Transactional administrator audit logging."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AdminAuditLog


async def record_admin_action(
    session: AsyncSession,
    *,
    admin_user_id: UUID | None,
    action: str,
    target_type: str,
    target_id: str,
    before_state: dict[str, Any] | None = None,
    after_state: dict[str, Any] | None = None,
    ip_address: str | None = None,
) -> None:
    """Insert an audit row without committing the caller's transaction."""
    session.add(
        AdminAuditLog(
            admin_user_id=admin_user_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            before_state=before_state,
            after_state=after_state,
            ip_address=ip_address,
        )
    )
    await session.flush()
