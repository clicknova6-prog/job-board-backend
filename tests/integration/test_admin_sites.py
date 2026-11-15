"""Integration tests for /admin/api/sites against a real PostgreSQL database."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import create_engine, delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app.api.routers import admin_sites
from app.auth.dependencies import get_current_admin
from app.auth.schemas import CurrentAdmin
from app.db.async_session import get_async_session
from app.db.models import (
    AdminAuditLog,
    AdminRole,
    AdminUser,
    Job,
    JobSite,
    Provider,
    Site,
)
from app.main import app
from app.services.site_routing_service import SiteRoutingService

NOW = datetime(2026, 11, 1, tzinfo=UTC)
SITE_BODY = {
    "slug": "it-tech",
    "name": "IT & Tech",
    "site_key": "it-tech-key-123",
    "base_url": "https://it.example.test/",
    "config": {"cors_origins": ["https://www.it.example.test"]},
}


def _job(index: int, title: str, classification: str | None = None) -> dict[str, Any]:
    return {
        "source_name": "jobg8",
        "source_job_id": f"adm-{index}",
        "slug": f"adm-job-{index}",
        "title": title,
        "description": "d",
        "classification": classification,
        "apply_url": f"https://example.test/{index}",
        "source_payload": {},
        "payload_hash": f"h{index}",
        "is_active": True,
        "deactivated_at": None,
        "first_imported_at": NOW,
        "last_imported_at": NOW,
    }


@pytest.fixture
def env(
    test_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> Iterator[SimpleNamespace]:
    sync_engine = create_engine(
        test_database_url.replace("postgresql://", "postgresql+psycopg://", 1),
        poolclass=NullPool,
    )
    sync_factory = sessionmaker(bind=sync_engine, expire_on_commit=False)
    admin_id = uuid4()
    with sync_factory() as s:
        s.execute(delete(Job))
        s.execute(delete(Site))
        s.execute(delete(AdminAuditLog))
        provider_id = s.scalar(select(Provider.id).where(Provider.name == "jobg8-test"))
        if provider_id is None:
            provider_id = s.scalar(
                insert(Provider)
                .values(name="jobg8-test", format="xml")
                .returning(Provider.id)
            )
        s.execute(
            insert(AdminUser).values(
                id=admin_id,
                email=f"{admin_id}@example.test",
                password_hash="x" * 60,
                role=AdminRole.ADMIN,
            )
        )
        titles = [
            "Python Developer",
            "Senior Nurse",
            "Java Developer Intern",
            "Accountant",
            "Network Engineer",
        ]
        s.execute(
            insert(Job),
            [dict(_job(i, t), provider_id=provider_id) for i, t in enumerate(titles)],
        )
        s.commit()

    # Preview and routing use the sync session factory.
    monkeypatch.setattr(
        admin_sites,
        "SiteRoutingService",
        lambda: SiteRoutingService(session_factory=sync_factory),
    )
    app.dependency_overrides[get_current_admin] = lambda: CurrentAdmin(
        id=admin_id, role=AdminRole.ADMIN
    )
    yield SimpleNamespace(
        url=test_database_url,
        sync_factory=sync_factory,
        provider_id=provider_id,
        admin_id=admin_id,
    )
    app.dependency_overrides.pop(get_current_admin, None)
    with sync_factory() as s:
        s.execute(delete(AdminAuditLog))
        s.execute(delete(AdminUser).where(AdminUser.id == admin_id))
        s.commit()
    sync_engine.dispose()


def _call(env: SimpleNamespace, steps) -> Any:
    async def run() -> Any:
        engine = create_async_engine(
            env.url.replace("postgresql://", "postgresql+asyncpg://", 1),
            poolclass=NullPool,
        )
        factory = async_sessionmaker(
            bind=engine, class_=AsyncSession, expire_on_commit=False
        )

        async def override() -> AsyncIterator[AsyncSession]:
            async with factory() as session:
                yield session

        app.dependency_overrides[get_async_session] = override
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                return await steps(client)
        finally:
            app.dependency_overrides.pop(get_async_session, None)
            await engine.dispose()

    return asyncio.run(run())


def _audit(env: SimpleNamespace) -> list[tuple[str, str]]:
    with env.sync_factory() as s:
        return [
            (row.action, row.target_type)
            for row in s.scalars(select(AdminAuditLog).order_by(AdminAuditLog.id))
        ]


def test_site_crud_is_validated_audited_and_conflict_safe(env: SimpleNamespace) -> None:
    async def steps(client: httpx.AsyncClient) -> None:
        created = await client.post("/admin/api/sites", json=SITE_BODY)
        assert created.status_code == 201, created.text
        site = created.json()
        assert site["base_url"] == "https://it.example.test"
        assert site["active_job_count"] == 0

        dup = await client.post(
            "/admin/api/sites", json={**SITE_BODY, "site_key": "other-key-1"}
        )
        assert dup.status_code == 409
        assert "slug" in dup.json()["error"]["message"]

        bad = await client.post(
            "/admin/api/sites",
            json={**SITE_BODY, "slug": "x", "config": {"cors_origins": ["nope"]}},
        )
        assert bad.status_code == 422

        patched = await client.patch(
            f"/admin/api/sites/{site['id']}",
            json={"name": "IT Jobs", "is_active": False},
        )
        assert patched.status_code == 200
        assert (patched.json()["name"], patched.json()["is_active"]) == (
            "IT Jobs",
            False,
        )
        assert (
            await client.patch(f"/admin/api/sites/{site['id']}", json={"slug": "y"})
        ).status_code == 422
        assert (await client.get("/admin/api/sites/999999")).status_code == 404
        listed = await client.get("/admin/api/sites")
        assert [s["slug"] for s in listed.json()] == ["it-tech"]

    _call(env, steps)
    assert _audit(env) == [("site.created", "site"), ("site.updated", "site")]


def test_rule_crud_preview_and_reroute(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    enqueued: list[str] = []

    class FakeTask:
        def delay(self) -> SimpleNamespace:
            enqueued.append("route_all_sites")
            return SimpleNamespace(id="task-123")

    from app.celery_app import celery_app

    monkeypatch.setitem(
        celery_app.tasks, "app.tasks.site_routing_tasks.route_all_sites", FakeTask()
    )

    async def steps(client: httpx.AsyncClient) -> dict[str, Any]:
        site_id = (await client.post("/admin/api/sites", json=SITE_BODY)).json()["id"]
        base = f"/admin/api/sites/{site_id}"
        dev = await client.post(
            f"{base}/rules",
            json={"rule_type": "include_title", "pattern": " developer "},
        )
        assert dev.status_code == 201
        assert dev.json()["pattern"] == "developer"
        assert (
            await client.post(
                f"{base}/rules",
                json={"rule_type": "include_title", "pattern": "Developer"},
            )
        ).status_code == 409
        assert (
            await client.post(
                f"{base}/rules",
                json={
                    "rule_type": "include_title",
                    "pattern": "x",
                    "provider_id": 999999,
                },
            )
        ).status_code == 422
        assert (
            await client.post(
                f"{base}/rules", json={"rule_type": "bogus", "pattern": "x"}
            )
        ).status_code == 422
        intern = await client.post(
            f"{base}/rules", json={"rule_type": "exclude_title", "pattern": "intern"}
        )
        assert intern.status_code == 201

        saved = (
            await client.post(f"{base}/routing/preview", json={"sample_size": 5})
        ).json()
        draft = (
            await client.post(
                f"{base}/routing/preview",
                json={
                    "rules": [
                        {"rule_type": "include_title", "pattern": "engineer"},
                        {"rule_type": "include_title", "pattern": "developer"},
                    ]
                },
            )
        ).json()

        patched = await client.patch(
            f"{base}/rules/{intern.json()['id']}", json={"is_active": False}
        )
        assert patched.json()["is_active"] is False
        assert (
            await client.delete(f"{base}/rules/{dev.json()['id']}")
        ).status_code == 204
        assert (
            await client.delete(f"{base}/rules/{dev.json()['id']}")
        ).status_code == 404
        rules = (await client.get(f"{base}/rules")).json()
        assert [r["pattern"] for r in rules] == ["intern"]

        run = await client.post("/admin/api/sites/routing/run")
        assert run.status_code == 202
        assert run.json() == {"task_id": "task-123", "status": "enqueued"}
        return {"saved": saved, "draft": draft}

    result = _call(env, steps)

    saved = result["saved"]
    assert (saved["jobs_evaluated"], saved["matched"], saved["would_add"]) == (5, 1, 1)
    assert saved["currently_assigned"] == saved["would_remove"] == 0
    assert saved["added_samples"] == ["Python Developer"]
    dev_rule, intern_rule = saved["rules"]
    assert (dev_rule["matched"], intern_rule["excluded"]) == (1, 1)
    assert dev_rule["rule_id"] is not None

    draft = result["draft"]
    assert draft["matched"] == 3
    assert [r["matched"] for r in draft["rules"]] == [1, 2]
    assert all(r["rule_id"] is None for r in draft["rules"])

    with env.sync_factory() as s:
        assert s.scalar(select(JobSite.job_id).limit(1)) is None, (
            "preview must not write"
        )
    assert enqueued == ["route_all_sites"]
    assert [a for a, _ in _audit(env)] == [
        "site.created",
        "site_routing_rule.created",
        "site_routing_rule.created",
        "site_routing_rule.updated",
        "site_routing_rule.deleted",
        "site_routing.triggered",
    ]


def test_admin_sites_routes_require_admin_role(env: SimpleNamespace) -> None:
    app.dependency_overrides[get_current_admin] = lambda: CurrentAdmin.model_construct(
        id=uuid4(), role="auditor"
    )

    async def steps(client: httpx.AsyncClient) -> list[int]:
        return [
            (await client.get("/admin/api/sites")).status_code,
            (await client.post("/admin/api/sites", json=SITE_BODY)).status_code,
            (await client.post("/admin/api/sites/routing/run")).status_code,
        ]

    assert _call(env, steps) == [403, 403, 403]
