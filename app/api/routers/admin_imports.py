"""JWT-protected import history and health administration routes."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.request_helpers import get_client_ip
from app.auth.dependencies import get_current_admin, require_admin_role
from app.auth.schemas import CurrentAdmin
from app.db.async_session import get_async_session
from app.db.import_repositories import (
    ImportRunRecord,
    ImportRunRepository,
)
from app.db.models import AdminRole
from app.db.provider_repositories import ProviderRepository
from app.schemas.import_run import (
    ImportRunRead,
    PaginatedImportRuns,
    PaginatedRejectedRecords,
)
from app.services.audit_log_service import record_admin_action

ImportRunStatus = Literal["pending", "processing", "completed", "failed"]
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200

router = APIRouter(
    prefix="/admin/api/imports",
    tags=["Admin Imports"],
    dependencies=[Depends(require_admin_role(AdminRole.ADMIN, AdminRole.SUPER_ADMIN))],
)


@router.get("", response_model=PaginatedImportRuns)
async def list_import_runs(
    session: Annotated[AsyncSession, Depends(get_async_session)],
    provider_id: Annotated[int | None, Query()] = None,
    status_filter: Annotated[ImportRunStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PaginatedImportRuns:
    """List import runs with optional provider and status filters."""
    repository = ImportRunRepository(session)
    items = await repository.list_import_runs(
        provider_id,
        status_filter,
        limit,
        offset,
    )
    total = await repository.count_import_runs(provider_id, status_filter)
    return PaginatedImportRuns(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{import_run_id}", response_model=ImportRunRead)
async def get_import_run(
    import_run_id: int,
    session: Annotated[AsyncSession, Depends(get_async_session)],
) -> ImportRunRecord:
    """Return one import run."""
    import_run = await ImportRunRepository(session).get_import_run(import_run_id)
    if import_run is None:
        raise _import_run_not_found()
    return import_run


@router.get("/{import_run_id}/rejected", response_model=PaginatedRejectedRecords)
async def list_rejected_records(
    import_run_id: int,
    session: Annotated[AsyncSession, Depends(get_async_session)],
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PaginatedRejectedRecords:
    """List rejected staging rows for one import run."""
    repository = ImportRunRepository(session)
    if await repository.get_import_run(import_run_id) is None:
        raise _import_run_not_found()
    items = await repository.list_rejected_records(import_run_id, limit, offset)
    total = await repository.count_rejected_records(import_run_id)
    return PaginatedRejectedRecords(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post("/providers/{provider_id}/trigger")
async def trigger_provider_import(
    provider_id: int,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_async_session)],
    current_admin: Annotated[CurrentAdmin, Depends(get_current_admin)],
) -> dict[str, int | str]:
    """Enqueue one active provider import and audit the administrator action."""
    provider = await ProviderRepository(session).get_provider(provider_id)
    if provider is None:
        raise _provider_not_found()
    if not provider.is_active:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot trigger an import for an inactive provider",
        )

    # Import the Celery app first so its forced autodiscovery can initialize
    # import_tasks before scheduler_tasks references run_provider_import.
    from app.celery_app import celery_app

    run_provider_import = celery_app.tasks["app.tasks.import_tasks.run_provider_import"]
    task = run_provider_import.delay(provider_id)
    task_id = str(task.id)
    await record_admin_action(
        session,
        admin_user_id=current_admin.id,
        action="import.manually_triggered",
        target_type="provider",
        target_id=str(provider_id),
        before_state=None,
        after_state={
            "task_id": task_id,
            "triggered_by": str(current_admin.id),
        },
        ip_address=get_client_ip(request),
    )
    await session.commit()
    return {
        "provider_id": provider_id,
        "task_id": task_id,
        "status": "enqueued",
    }


def _import_run_not_found() -> HTTPException:
    """Build the common import-run-not-found response."""
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Import run not found",
    )


def _provider_not_found() -> HTTPException:
    """Build the common provider-not-found response."""
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Provider not found",
    )
