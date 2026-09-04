"""JWT-protected provider administration routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.request_helpers import get_client_ip
from app.auth.dependencies import get_current_admin, require_admin_role
from app.auth.schemas import CurrentAdmin
from app.core.rate_limit import limiter, rate_limit_settings
from app.db.async_session import get_async_session
from app.db.models import AdminRole
from app.db.provider_repositories import ProviderRecord, ProviderRepository
from app.schemas.provider import ProviderRead, ProviderUpdate
from app.services.audit_log_service import record_admin_action

router = APIRouter(
    prefix="/admin/api/providers",
    tags=["Admin Providers"],
    dependencies=[Depends(require_admin_role(AdminRole.ADMIN, AdminRole.SUPER_ADMIN))],
)


@router.get("", response_model=list[ProviderRead])
@limiter.limit(rate_limit_settings.admin_api)
async def list_providers(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_async_session)],
) -> list[ProviderRecord]:
    """List all configured providers."""
    return await ProviderRepository(session).list_providers()


@router.get("/{provider_id}", response_model=ProviderRead)
@limiter.limit(rate_limit_settings.admin_api)
async def get_provider(
    provider_id: int,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_async_session)],
) -> ProviderRecord:
    """Return one provider configuration."""
    provider = await ProviderRepository(session).get_provider(provider_id)
    if provider is None:
        raise _provider_not_found()
    return provider


@router.patch("/{provider_id}", response_model=ProviderRead)
@limiter.limit(rate_limit_settings.admin_api)
async def update_provider(
    provider_id: int,
    update: ProviderUpdate,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_async_session)],
    current_admin: Annotated[CurrentAdmin, Depends(get_current_admin)],
) -> ProviderRecord:
    """Partially update a provider and audit the transactional change."""
    states = await ProviderRepository(session).update_provider(
        provider_id,
        **update.model_dump(exclude_unset=True),
    )
    return await _audit_and_commit(
        session,
        states,
        current_admin=current_admin,
        action="provider.updated",
        provider_id=provider_id,
        ip_address=get_client_ip(request),
    )


@router.post("/{provider_id}/activate", response_model=ProviderRead)
@limiter.limit(rate_limit_settings.admin_api)
async def activate_provider(
    provider_id: int,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_async_session)],
    current_admin: Annotated[CurrentAdmin, Depends(get_current_admin)],
) -> ProviderRecord:
    """Activate a provider and audit the transactional change."""
    states = await ProviderRepository(session).set_provider_active(provider_id, True)
    return await _audit_and_commit(
        session,
        states,
        current_admin=current_admin,
        action="provider.activated",
        provider_id=provider_id,
        ip_address=get_client_ip(request),
    )


@router.post("/{provider_id}/deactivate", response_model=ProviderRead)
@limiter.limit(rate_limit_settings.admin_api)
async def deactivate_provider(
    provider_id: int,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_async_session)],
    current_admin: Annotated[CurrentAdmin, Depends(get_current_admin)],
) -> ProviderRecord:
    """Deactivate a provider and audit the transactional change."""
    states = await ProviderRepository(session).set_provider_active(provider_id, False)
    return await _audit_and_commit(
        session,
        states,
        current_admin=current_admin,
        action="provider.deactivated",
        provider_id=provider_id,
        ip_address=get_client_ip(request),
    )


async def _audit_and_commit(
    session: AsyncSession,
    states: tuple[ProviderRecord, ProviderRecord] | None,
    *,
    current_admin: CurrentAdmin,
    action: str,
    provider_id: int,
    ip_address: str | None,
) -> ProviderRecord:
    """Audit and commit one provider mutation, or raise a not-found response."""
    if states is None:
        raise _provider_not_found()
    before, after = states
    await record_admin_action(
        session,
        admin_user_id=current_admin.id,
        action=action,
        target_type="provider",
        target_id=str(provider_id),
        before_state=ProviderRead.model_validate(before).model_dump(mode="json"),
        after_state=ProviderRead.model_validate(after).model_dump(mode="json"),
        ip_address=ip_address,
    )
    await session.commit()
    return after


def _provider_not_found() -> HTTPException:
    """Build the common provider-not-found response."""
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Provider not found",
    )
