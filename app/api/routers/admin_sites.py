"""JWT-protected administration of sites and their routing rules.

Rule edits take effect on the next routing pass: after every completed
import, or immediately via ``POST /admin/api/sites/routing/run``. Use
``POST /admin/api/sites/{id}/routing/preview`` first to dry-run edits.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.request_helpers import get_client_ip
from app.auth.dependencies import get_current_admin, require_admin_role
from app.auth.schemas import CurrentAdmin
from app.core.rate_limit import limiter, rate_limit_settings
from app.core.site_cors import site_origin_registry
from app.db.async_session import get_async_session
from app.db.models import AdminRole
from app.db.site_admin_repositories import (
    RoutingRuleRecord,
    SiteAdminConflictError,
    SiteAdminRecord,
    SiteAdminRepository,
)
from app.schemas.site import (
    RoutingPreviewOut,
    RoutingPreviewRequest,
    RoutingRuleDraft,
    RoutingRuleRead,
    RoutingRuleUpdate,
    SiteCreate,
    SiteRead,
    SiteUpdate,
)
from app.services.audit_log_service import record_admin_action
from app.services.site_routing_service import DraftRule, SiteRoutingService

router = APIRouter(
    prefix="/admin/api/sites",
    tags=["Admin Sites"],
    dependencies=[Depends(require_admin_role(AdminRole.ADMIN, AdminRole.SUPER_ADMIN))],
)

Session = Annotated[AsyncSession, Depends(get_async_session)]
Admin = Annotated[CurrentAdmin, Depends(get_current_admin)]


def _not_found(what: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND, detail=f"{what} not found"
    )


def _conflict(exc: SiteAdminConflictError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


def _site_state(record: SiteAdminRecord) -> dict[str, Any]:
    return SiteRead.model_validate(record).model_dump(
        mode="json", exclude={"active_job_count", "rule_count"}
    )


def _rule_state(record: RoutingRuleRecord) -> dict[str, Any]:
    return RoutingRuleRead.model_validate(record).model_dump(mode="json")


async def _audit_commit(
    session: AsyncSession,
    request: Request,
    admin: CurrentAdmin,
    *,
    action: str,
    target_type: str,
    target_id: int | str,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> None:
    await record_admin_action(
        session,
        admin_user_id=admin.id,
        action=action,
        target_type=target_type,
        target_id=str(target_id),
        before_state=before,
        after_state=after,
        ip_address=get_client_ip(request),
    )
    await session.commit()


async def _require_site(repo: SiteAdminRepository, site_id: int) -> None:
    if not await repo.site_exists(site_id):
        raise _not_found("Site")


async def _require_provider(repo: SiteAdminRepository, provider_id: int | None) -> None:
    if provider_id is not None and not await repo.provider_exists(provider_id):
        raise HTTPException(status_code=422, detail="Unknown provider")


# ---- sites ---------------------------------------------------------------


@router.get("", response_model=list[SiteRead])
@limiter.limit(rate_limit_settings.admin_api)
async def list_sites(request: Request, session: Session) -> list[SiteAdminRecord]:
    """List all sites with their active-job and rule counts."""
    return await SiteAdminRepository(session).list_sites()


@router.post("", response_model=SiteRead, status_code=status.HTTP_201_CREATED)
@limiter.limit(rate_limit_settings.admin_api)
async def create_site(
    body: SiteCreate, request: Request, session: Session, admin: Admin
) -> SiteAdminRecord:
    """Create a site. It gets jobs on the next routing pass once it has rules."""
    repo = SiteAdminRepository(session)
    try:
        site = await repo.create_site(**body.model_dump())
    except SiteAdminConflictError as exc:
        raise _conflict(exc) from exc
    await _audit_commit(
        session,
        request,
        admin,
        action="site.created",
        target_type="site",
        target_id=site.id,
        before=None,
        after=_site_state(site),
    )
    site_origin_registry.invalidate()
    return site


@router.get("/{site_id}", response_model=SiteRead)
@limiter.limit(rate_limit_settings.admin_api)
async def get_site(site_id: int, request: Request, session: Session) -> SiteAdminRecord:
    """Return one site."""
    site = await SiteAdminRepository(session).get_site(site_id)
    if site is None:
        raise _not_found("Site")
    return site


@router.patch("/{site_id}", response_model=SiteRead)
@limiter.limit(rate_limit_settings.admin_api)
async def update_site(
    site_id: int, body: SiteUpdate, request: Request, session: Session, admin: Admin
) -> SiteAdminRecord:
    """Partially update a site (including ``is_active``); slug is immutable."""
    repo = SiteAdminRepository(session)
    try:
        states = await repo.update_site(site_id, **body.model_dump(exclude_unset=True))
    except SiteAdminConflictError as exc:
        raise _conflict(exc) from exc
    if states is None:
        raise _not_found("Site")
    before, after = states
    await _audit_commit(
        session,
        request,
        admin,
        action="site.updated",
        target_type="site",
        target_id=site_id,
        before=_site_state(before),
        after=_site_state(after),
    )
    site_origin_registry.invalidate()
    return await repo.get_site(site_id) or after


# ---- routing -------------------------------------------------------------


@router.post("/routing/run", status_code=status.HTTP_202_ACCEPTED)
@limiter.limit(rate_limit_settings.admin_import_trigger)
async def run_site_routing(
    request: Request, session: Session, admin: Admin
) -> dict[str, str]:
    """Enqueue a full re-route of all providers' jobs to all active sites."""
    # Import the Celery app first so forced autodiscovery registers the task.
    from app.celery_app import celery_app

    task = celery_app.tasks["app.tasks.site_routing_tasks.route_all_sites"].delay()
    task_id = str(task.id)
    await _audit_commit(
        session,
        request,
        admin,
        action="site_routing.triggered",
        target_type="site_routing",
        target_id="all",
        before=None,
        after={"task_id": task_id},
    )
    return {"task_id": task_id, "status": "enqueued"}


@router.post("/{site_id}/routing/preview", response_model=RoutingPreviewOut)
@limiter.limit(rate_limit_settings.admin_import_trigger)
async def preview_site_routing(
    site_id: int, body: RoutingPreviewRequest, request: Request, session: Session
):
    """Dry-run the site's saved rules, or a draft rule list, over all jobs.

    Writes nothing. Evaluates every job, so expect a few seconds on a full feed.
    """
    repo = SiteAdminRepository(session)
    await _require_site(repo, site_id)
    drafts = None
    if body.rules is not None:
        drafts = [
            DraftRule(rule.rule_type, rule.pattern, rule.provider_id)
            for rule in body.rules
            if rule.is_active
        ]
    return await run_in_threadpool(
        SiteRoutingService().preview_site,
        site_id,
        draft_rules=drafts,
        sample_size=body.sample_size,
    )


# ---- rules ---------------------------------------------------------------


@router.get("/{site_id}/rules", response_model=list[RoutingRuleRead])
@limiter.limit(rate_limit_settings.admin_api)
async def list_rules(
    site_id: int, request: Request, session: Session
) -> list[RoutingRuleRecord]:
    """List a site's routing rules (active and inactive) in precedence order."""
    repo = SiteAdminRepository(session)
    await _require_site(repo, site_id)
    return await repo.list_rules(site_id)


@router.post(
    "/{site_id}/rules",
    response_model=RoutingRuleRead,
    status_code=status.HTTP_201_CREATED,
)
@limiter.limit(rate_limit_settings.admin_api)
async def create_rule(
    site_id: int,
    body: RoutingRuleDraft,
    request: Request,
    session: Session,
    admin: Admin,
) -> RoutingRuleRecord:
    """Add a routing rule to a site."""
    repo = SiteAdminRepository(session)
    await _require_site(repo, site_id)
    await _require_provider(repo, body.provider_id)
    try:
        rule = await repo.create_rule(site_id, **body.model_dump())
    except SiteAdminConflictError as exc:
        raise _conflict(exc) from exc
    await _audit_commit(
        session,
        request,
        admin,
        action="site_routing_rule.created",
        target_type="site_routing_rule",
        target_id=rule.id,
        before=None,
        after=_rule_state(rule),
    )
    return rule


@router.patch("/{site_id}/rules/{rule_id}", response_model=RoutingRuleRead)
@limiter.limit(rate_limit_settings.admin_api)
async def update_rule(
    site_id: int,
    rule_id: int,
    body: RoutingRuleUpdate,
    request: Request,
    session: Session,
    admin: Admin,
) -> RoutingRuleRecord:
    """Partially update a routing rule."""
    repo = SiteAdminRepository(session)
    fields = body.model_dump(exclude_unset=True)
    await _require_provider(repo, fields.get("provider_id"))
    try:
        states = await repo.update_rule(site_id, rule_id, **fields)
    except SiteAdminConflictError as exc:
        raise _conflict(exc) from exc
    if states is None:
        raise _not_found("Rule")
    before, after = states
    await _audit_commit(
        session,
        request,
        admin,
        action="site_routing_rule.updated",
        target_type="site_routing_rule",
        target_id=rule_id,
        before=_rule_state(before),
        after=_rule_state(after),
    )
    return after


@router.delete("/{site_id}/rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit(rate_limit_settings.admin_api)
async def delete_rule(
    site_id: int, rule_id: int, request: Request, session: Session, admin: Admin
) -> None:
    """Delete a routing rule (jobs it assigned keep their site until re-route)."""
    repo = SiteAdminRepository(session)
    before = await repo.delete_rule(site_id, rule_id)
    if before is None:
        raise _not_found("Rule")
    await _audit_commit(
        session,
        request,
        admin,
        action="site_routing_rule.deleted",
        target_type="site_routing_rule",
        target_id=rule_id,
        before=_rule_state(before),
        after=None,
    )
