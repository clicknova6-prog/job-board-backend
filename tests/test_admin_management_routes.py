"""ASGI tests for provider and import-run administration routes."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator, Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routers import admin_imports, admin_providers
from app.auth.dependencies import get_current_admin, get_jwt_service
from app.auth.schemas import CurrentAdmin
from app.core.auth_config import JWTSettings
from app.db.async_session import get_async_session
from app.db.import_repositories import (
    ImportRunRecord,
    ImportRunRepository,
    RejectedRecordRow,
)
from app.db.models import AdminRole
from app.db.provider_repositories import ProviderRecord, ProviderRepository
from app.main import app
from app.schemas.provider import ProviderRead
from app.services.auth.jwt_service import JWTService

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


class _SessionStub:
    def __init__(self) -> None:
        self.commit_count = 0

    async def commit(self) -> None:
        self.commit_count += 1


@pytest.fixture(autouse=True)
def _reset_dependency_overrides() -> Iterator[None]:
    app.dependency_overrides.clear()
    yield
    app.dependency_overrides.clear()


def _jwt_service() -> JWTService:
    return JWTService(
        object(),  # type: ignore[arg-type]
        settings=JWTSettings(
            secret="admin-management-route-test-secret-32-bytes",
            algorithm="HS256",
        ),
    )


def _admin_credentials(jwt_service: JWTService) -> tuple[UUID, dict[str, str]]:
    admin_id = uuid4()
    token = jwt_service.issue_access_token(
        admin_id,
        "admin",
        role=AdminRole.ADMIN,
    )
    return admin_id, {"Authorization": f"Bearer {token}"}


def _provider(
    provider_id: int = 7,
    *,
    is_active: bool = True,
    feed_url: str | None = "https://feeds.example/jobs.xml",
    updated_at: datetime = NOW,
) -> ProviderRecord:
    return ProviderRecord(
        id=provider_id,
        name="jobg8",
        feed_url=feed_url,
        format="xml",
        archive_type="zip",
        schedule_cron="0 * * * *",
        timeout_seconds=60,
        retry_max_attempts=3,
        is_active=is_active,
        config={"anomaly_drop_threshold_pct": 20},
        created_at=NOW - timedelta(days=1),
        updated_at=updated_at,
        schedule_interval_minutes=60,
        deleted_job_retention_hours=12,
    )


def _import_run(import_run_id: int = 11) -> ImportRunRecord:
    return ImportRunRecord(
        id=import_run_id,
        provider_id=7,
        source_name="jobg8",
        source_uri="https://feeds.example/jobs.xml",
        source_checksum="abc123",
        status="failed",
        started_at=NOW - timedelta(minutes=5),
        completed_at=NOW,
        records_received=10,
        records_staged=8,
        records_imported=0,
        records_rejected=2,
        new_jobs=0,
        updated_jobs=0,
        deleted_jobs=0,
        unmapped_fields={"NewField": 1},
        field_fallback_warnings={"employment_type": 1},
        is_anomalous=True,
        anomaly_reasons=["high_rejection_rate"],
        error_message="Promotion aborted",
        created_at=NOW - timedelta(minutes=5),
        updated_at=NOW,
    )


def _rejected_record(import_run_id: int = 11) -> RejectedRecordRow:
    return RejectedRecordRow(
        id=91,
        import_run_id=import_run_id,
        source_job_id="bad-job",
        title=None,
        validation_errors=[{"field": "Position", "message": "Field required"}],
        staged_at=NOW,
    )


def _configure_dependencies(
    session: _SessionStub,
    jwt_service: JWTService,
) -> None:
    async def override_session() -> AsyncIterator[AsyncSession]:
        yield cast(AsyncSession, session)

    app.dependency_overrides[get_async_session] = override_session
    app.dependency_overrides[get_jwt_service] = lambda: jwt_service


def _request(
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    json: dict[str, Any] | None = None,
    client_host: str = "testclient",
) -> httpx.Response:
    async def send() -> httpx.Response:
        transport = httpx.ASGITransport(app=app, client=(client_host, 123))
        async with httpx.AsyncClient(
            transport=transport,
            base_url="https://testserver",
            follow_redirects=False,
        ) as client:
            return await client.request(method, path, headers=headers, json=json)

    return asyncio.run(send())


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/admin/api/imports", None),
        ("GET", "/admin/api/imports/11", None),
        ("GET", "/admin/api/imports/11/rejected", None),
        ("POST", "/admin/api/imports/providers/7/trigger", None),
        ("GET", "/admin/api/providers", None),
        ("GET", "/admin/api/providers/7", None),
        ("PATCH", "/admin/api/providers/7", {"timeout_seconds": 30}),
        ("POST", "/admin/api/providers/7/activate", None),
        ("POST", "/admin/api/providers/7/deactivate", None),
    ],
)
def test_admin_management_routes_enforce_authentication_and_role(
    method: str,
    path: str,
    body: dict[str, Any] | None,
) -> None:
    jwt_service = _jwt_service()
    _configure_dependencies(_SessionStub(), jwt_service)

    unauthenticated = _request(method, path, json=body)
    user_token = jwt_service.issue_access_token(uuid4(), "user")
    user_scoped = _request(
        method,
        path,
        headers={"Authorization": f"Bearer {user_token}"},
        json=body,
    )

    disallowed_admin = CurrentAdmin.model_construct(id=uuid4(), role="auditor")
    app.dependency_overrides[get_current_admin] = lambda: disallowed_admin
    forbidden = _request(method, path, json=body)

    assert unauthenticated.status_code == 401
    assert user_scoped.status_code == 401
    assert forbidden.status_code == 403
    assert forbidden.json() == {"detail": "Insufficient administrator role"}


def test_list_import_runs_applies_filters_and_pagination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Any, ...]] = []
    record = _import_run()

    async def list_runs(
        self: ImportRunRepository,
        provider_id: int | None,
        status: str | None,
        limit: int,
        offset: int,
    ) -> list[ImportRunRecord]:
        calls.append(("list", provider_id, status, limit, offset))
        return [record]

    async def count_runs(
        self: ImportRunRepository,
        provider_id: int | None,
        status: str | None,
    ) -> int:
        calls.append(("count", provider_id, status))
        return 4

    monkeypatch.setattr(ImportRunRepository, "list_import_runs", list_runs)
    monkeypatch.setattr(ImportRunRepository, "count_import_runs", count_runs)
    jwt_service = _jwt_service()
    _configure_dependencies(_SessionStub(), jwt_service)
    _, headers = _admin_credentials(jwt_service)

    response = _request(
        "GET",
        "/admin/api/imports?provider_id=7&status=failed&limit=2&offset=3",
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["items"][0]["id"] == record.id
    assert response.json()["total"] == 4
    assert response.json()["limit"] == 2
    assert response.json()["offset"] == 3
    assert calls == [
        ("list", 7, "failed", 2, 3),
        ("count", 7, "failed"),
    ]


def test_get_import_run_success_and_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def get_run(
        self: ImportRunRepository,
        import_run_id: int,
    ) -> ImportRunRecord | None:
        return _import_run(import_run_id) if import_run_id == 11 else None

    monkeypatch.setattr(ImportRunRepository, "get_import_run", get_run)
    jwt_service = _jwt_service()
    _configure_dependencies(_SessionStub(), jwt_service)
    _, headers = _admin_credentials(jwt_service)

    found = _request("GET", "/admin/api/imports/11", headers=headers)
    missing = _request("GET", "/admin/api/imports/404", headers=headers)

    assert found.status_code == 200
    assert found.json()["id"] == 11
    assert missing.status_code == 404
    assert missing.json() == {"detail": "Import run not found"}


def test_list_rejected_records_success_and_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Any, ...]] = []

    async def get_run(
        self: ImportRunRepository,
        import_run_id: int,
    ) -> ImportRunRecord | None:
        return _import_run(import_run_id) if import_run_id == 11 else None

    async def list_rejected(
        self: ImportRunRepository,
        import_run_id: int,
        limit: int,
        offset: int,
    ) -> list[RejectedRecordRow]:
        calls.append(("list", import_run_id, limit, offset))
        return [_rejected_record(import_run_id)]

    async def count_rejected(
        self: ImportRunRepository,
        import_run_id: int,
    ) -> int:
        calls.append(("count", import_run_id))
        return 1

    monkeypatch.setattr(ImportRunRepository, "get_import_run", get_run)
    monkeypatch.setattr(ImportRunRepository, "list_rejected_records", list_rejected)
    monkeypatch.setattr(ImportRunRepository, "count_rejected_records", count_rejected)
    jwt_service = _jwt_service()
    _configure_dependencies(_SessionStub(), jwt_service)
    _, headers = _admin_credentials(jwt_service)

    found = _request(
        "GET",
        "/admin/api/imports/11/rejected?limit=10&offset=2",
        headers=headers,
    )
    missing = _request(
        "GET",
        "/admin/api/imports/404/rejected",
        headers=headers,
    )

    assert found.status_code == 200
    assert found.json() == {
        "items": [
            {
                "id": 91,
                "import_run_id": 11,
                "source_job_id": "bad-job",
                "title": None,
                "validation_errors": [
                    {"field": "Position", "message": "Field required"}
                ],
                "staged_at": NOW.isoformat().replace("+00:00", "Z"),
            }
        ],
        "total": 1,
        "limit": 10,
        "offset": 2,
    }
    assert missing.status_code == 404
    assert calls == [("list", 11, 10, 2), ("count", 11)]


@pytest.mark.parametrize(
    "path",
    [
        "/admin/api/imports?status=unknown",
        "/admin/api/imports?limit=201",
        "/admin/api/imports/11/rejected?offset=-1",
    ],
)
def test_import_route_query_validation(path: str) -> None:
    jwt_service = _jwt_service()
    _configure_dependencies(_SessionStub(), jwt_service)
    _, headers = _admin_credentials(jwt_service)

    assert _request("GET", path, headers=headers).status_code == 422


def test_trigger_provider_import_enqueues_and_audits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def get_provider(
        self: ProviderRepository,
        provider_id: int,
    ) -> ProviderRecord | None:
        return _provider(provider_id)

    audit = AsyncMock()
    task = Mock()
    task.delay.return_value = SimpleNamespace(id="task-123")
    fake_celery_module = SimpleNamespace(
        celery_app=SimpleNamespace(
            tasks={"app.tasks.import_tasks.run_provider_import": task}
        )
    )
    monkeypatch.setattr(ProviderRepository, "get_provider", get_provider)
    monkeypatch.setattr(admin_imports, "record_admin_action", audit)
    monkeypatch.setitem(sys.modules, "app.celery_app", fake_celery_module)
    jwt_service = _jwt_service()
    session = _SessionStub()
    _configure_dependencies(session, jwt_service)
    admin_id, headers = _admin_credentials(jwt_service)

    response = _request(
        "POST",
        "/admin/api/imports/providers/7/trigger",
        headers=headers,
        client_host="203.0.113.10",
    )

    assert response.status_code == 200
    assert response.json() == {
        "provider_id": 7,
        "task_id": "task-123",
        "status": "enqueued",
    }
    task.delay.assert_called_once_with(7)
    audit.assert_awaited_once_with(
        cast(AsyncSession, session),
        admin_user_id=admin_id,
        action="import.manually_triggered",
        target_type="provider",
        target_id="7",
        before_state=None,
        after_state={"task_id": "task-123", "triggered_by": str(admin_id)},
        ip_address="203.0.113.10",
    )
    assert session.commit_count == 1


@pytest.mark.parametrize(
    ("provider", "expected_status", "expected_detail"),
    [
        (None, 404, "Provider not found"),
        (_provider(is_active=False), 409, "Cannot trigger an import for an inactive provider"),
    ],
)
def test_trigger_provider_import_rejects_unavailable_provider(
    monkeypatch: pytest.MonkeyPatch,
    provider: ProviderRecord | None,
    expected_status: int,
    expected_detail: str,
) -> None:
    async def get_provider(
        self: ProviderRepository,
        provider_id: int,
    ) -> ProviderRecord | None:
        return provider

    monkeypatch.setattr(ProviderRepository, "get_provider", get_provider)
    jwt_service = _jwt_service()
    session = _SessionStub()
    _configure_dependencies(session, jwt_service)
    _, headers = _admin_credentials(jwt_service)

    response = _request(
        "POST",
        "/admin/api/imports/providers/7/trigger",
        headers=headers,
    )

    assert response.status_code == expected_status
    assert response.json() == {"detail": expected_detail}
    assert session.commit_count == 0


def test_list_and_get_providers_success_and_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider()

    async def list_providers(self: ProviderRepository) -> list[ProviderRecord]:
        return [provider]

    async def get_provider(
        self: ProviderRepository,
        provider_id: int,
    ) -> ProviderRecord | None:
        return provider if provider_id == provider.id else None

    monkeypatch.setattr(ProviderRepository, "list_providers", list_providers)
    monkeypatch.setattr(ProviderRepository, "get_provider", get_provider)
    jwt_service = _jwt_service()
    _configure_dependencies(_SessionStub(), jwt_service)
    _, headers = _admin_credentials(jwt_service)

    listed = _request("GET", "/admin/api/providers", headers=headers)
    found = _request("GET", "/admin/api/providers/7", headers=headers)
    missing = _request("GET", "/admin/api/providers/404", headers=headers)

    assert listed.status_code == 200
    assert listed.json()[0]["id"] == 7
    assert found.status_code == 200
    assert found.json()["name"] == "jobg8"
    assert missing.status_code == 404
    assert missing.json() == {"detail": "Provider not found"}


def test_patch_provider_updates_only_supplied_fields_and_audits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _provider()
    after = replace(
        before,
        feed_url="https://new.example/feed.xml",
        timeout_seconds=90,
        updated_at=NOW + timedelta(minutes=1),
    )
    update_calls: list[tuple[int, dict[str, Any]]] = []

    async def update_provider(
        self: ProviderRepository,
        provider_id: int,
        **fields: Any,
    ) -> tuple[ProviderRecord, ProviderRecord] | None:
        update_calls.append((provider_id, fields))
        return before, after

    audit = AsyncMock()
    monkeypatch.setattr(ProviderRepository, "update_provider", update_provider)
    monkeypatch.setattr(admin_providers, "record_admin_action", audit)
    jwt_service = _jwt_service()
    session = _SessionStub()
    _configure_dependencies(session, jwt_service)
    admin_id, headers = _admin_credentials(jwt_service)

    response = _request(
        "PATCH",
        "/admin/api/providers/7",
        headers=headers,
        json={
            "feed_url": "https://new.example/feed.xml",
            "timeout_seconds": 90,
        },
        client_host="198.51.100.20",
    )

    assert response.status_code == 200
    assert response.json()["feed_url"] == "https://new.example/feed.xml"
    assert update_calls == [
        (
            7,
            {
                "feed_url": "https://new.example/feed.xml",
                "timeout_seconds": 90,
            },
        )
    ]
    audit.assert_awaited_once_with(
        cast(AsyncSession, session),
        admin_user_id=admin_id,
        action="provider.updated",
        target_type="provider",
        target_id="7",
        before_state=ProviderRead.model_validate(before).model_dump(mode="json"),
        after_state=ProviderRead.model_validate(after).model_dump(mode="json"),
        ip_address="198.51.100.20",
    )
    assert session.commit_count == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"unknown_field": "value"},
        {"timeout_seconds": "not-an-integer"},
        {"config": "not-an-object"},
    ],
)
def test_patch_provider_rejects_invalid_payload(payload: dict[str, Any]) -> None:
    jwt_service = _jwt_service()
    _configure_dependencies(_SessionStub(), jwt_service)
    _, headers = _admin_credentials(jwt_service)

    response = _request(
        "PATCH",
        "/admin/api/providers/7",
        headers=headers,
        json=payload,
    )

    assert response.status_code == 422


def test_patch_provider_returns_not_found_without_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def update_provider(
        self: ProviderRepository,
        provider_id: int,
        **fields: Any,
    ) -> tuple[ProviderRecord, ProviderRecord] | None:
        return None

    audit = AsyncMock()
    monkeypatch.setattr(ProviderRepository, "update_provider", update_provider)
    monkeypatch.setattr(admin_providers, "record_admin_action", audit)
    jwt_service = _jwt_service()
    session = _SessionStub()
    _configure_dependencies(session, jwt_service)
    _, headers = _admin_credentials(jwt_service)

    response = _request(
        "PATCH",
        "/admin/api/providers/404",
        headers=headers,
        json={"timeout_seconds": 90},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Provider not found"}
    audit.assert_not_awaited()
    assert session.commit_count == 0


@pytest.mark.parametrize(
    ("endpoint", "requested_active", "action"),
    [
        ("activate", True, "provider.activated"),
        ("deactivate", False, "provider.deactivated"),
    ],
)
def test_activate_and_deactivate_provider_are_audited(
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
    requested_active: bool,
    action: str,
) -> None:
    before = _provider(is_active=not requested_active)
    after = replace(
        before,
        is_active=requested_active,
        updated_at=NOW + timedelta(minutes=1),
    )
    active_calls: list[tuple[int, bool]] = []

    async def set_provider_active(
        self: ProviderRepository,
        provider_id: int,
        is_active: bool,
    ) -> tuple[ProviderRecord, ProviderRecord] | None:
        active_calls.append((provider_id, is_active))
        return before, after

    audit = AsyncMock()
    monkeypatch.setattr(
        ProviderRepository,
        "set_provider_active",
        set_provider_active,
    )
    monkeypatch.setattr(admin_providers, "record_admin_action", audit)
    jwt_service = _jwt_service()
    session = _SessionStub()
    _configure_dependencies(session, jwt_service)
    admin_id, headers = _admin_credentials(jwt_service)

    response = _request(
        "POST",
        f"/admin/api/providers/7/{endpoint}",
        headers=headers,
        client_host="192.0.2.30",
    )

    assert response.status_code == 200
    assert response.json()["is_active"] is requested_active
    assert active_calls == [(7, requested_active)]
    audit.assert_awaited_once_with(
        cast(AsyncSession, session),
        admin_user_id=admin_id,
        action=action,
        target_type="provider",
        target_id="7",
        before_state=ProviderRead.model_validate(before).model_dump(mode="json"),
        after_state=ProviderRead.model_validate(after).model_dump(mode="json"),
        ip_address="192.0.2.30",
    )
    assert session.commit_count == 1


@pytest.mark.parametrize("endpoint", ["activate", "deactivate"])
def test_activate_and_deactivate_missing_provider_return_not_found(
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
) -> None:
    async def set_provider_active(
        self: ProviderRepository,
        provider_id: int,
        is_active: bool,
    ) -> tuple[ProviderRecord, ProviderRecord] | None:
        return None

    audit = AsyncMock()
    monkeypatch.setattr(
        ProviderRepository,
        "set_provider_active",
        set_provider_active,
    )
    monkeypatch.setattr(admin_providers, "record_admin_action", audit)
    jwt_service = _jwt_service()
    session = _SessionStub()
    _configure_dependencies(session, jwt_service)
    _, headers = _admin_credentials(jwt_service)

    response = _request(
        "POST",
        f"/admin/api/providers/404/{endpoint}",
        headers=headers,
    )

    assert response.status_code == 404
    audit.assert_not_awaited()
    assert session.commit_count == 0
