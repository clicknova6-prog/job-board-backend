"""Unit tests for affiliate-link persistence and business logic."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from app.api.routers import admin_affiliate
from app.db.affiliate_repositories import AffiliateRepository
from app.schemas.affiliate import AffiliateGenerateRequest
from app.services import affiliate_service

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


def _session() -> AsyncMock:
    return AsyncMock(spec=AsyncSession)


def _result(rows: list[dict[str, object]]) -> Mock:
    result = Mock()
    result.mappings.return_value = rows
    return result


def _integrity_error(
    *,
    constraint_name: str | None = None,
    message: str = "duplicate key",
) -> IntegrityError:
    original = Exception(message)
    if constraint_name is not None:
        original.constraint_name = constraint_name  # type: ignore[attr-defined]
    return IntegrityError("INSERT", {}, original)


class _AffiliateRepositoryStub:
    """Model provider-scoped jobs and idempotent affiliate links in memory."""

    def __init__(self, jobs: list[dict[str, object]]) -> None:
        self.jobs = jobs
        self.links_by_job: dict[int, dict[str, object]] = {}
        self.lookup_calls: list[tuple[int, list[str]]] = []
        self.create_calls: list[list[dict[str, object]]] = []

    async def lookup_jobs_by_references(
        self,
        provider_id: int,
        source_job_ids: list[str],
    ) -> list[dict[str, object]]:
        self.lookup_calls.append((provider_id, list(source_job_ids)))
        return [
            dict(job)
            for job in self.jobs
            if job["provider_id"] == provider_id
            and job["source_job_id"] in source_job_ids
        ]

    async def create_affiliate_links(
        self,
        links: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        self.create_calls.append([dict(link) for link in links])
        for link in links:
            job_id = int(link["job_id"])
            self.links_by_job.setdefault(
                job_id,
                {
                    "id": len(self.links_by_job) + 1,
                    **link,
                    "created_at": NOW,
                },
            )
        requested_ids = list(dict.fromkeys(int(link["job_id"]) for link in links))
        return [self.links_by_job[job_id] for job_id in sorted(requested_ids)]


def _job(
    job_id: int,
    provider_id: int,
    source_job_id: str,
    *,
    apply_url: str | None = "https://example.test/apply",
) -> dict[str, object]:
    return {
        "id": job_id,
        "provider_id": provider_id,
        "source_job_id": source_job_id,
        "title": f"Job {job_id}",
        "advertiser_name": "Acme",
        "apply_url": apply_url,
        "is_active": True,
        "has_affiliate_link": False,
        "short_hash": None,
    }


def test_repository_empty_inputs_do_not_query_database() -> None:
    session = _session()
    repository = AffiliateRepository(session)

    async def run() -> None:
        assert await repository.lookup_jobs_by_references(7, []) == []
        assert await repository.lookup_jobs_by_ids(7, []) == []
        assert await repository.create_affiliate_links([]) == []

    asyncio.run(run())
    session.execute.assert_not_awaited()


def test_repository_lookup_is_scoped_by_provider_and_sender_references() -> None:
    session = _session()
    rows = [
        {
            "id": 11,
            "source_job_id": "sender-1",
            "title": "Engineer",
            "advertiser_name": "Acme",
            "apply_url": "https://example.test/jobs/11",
            "is_active": True,
            "has_affiliate_link": False,
            "short_hash": None,
        }
    ]
    session.execute.return_value = _result(rows)

    async def run() -> None:
        result = await AffiliateRepository(session).lookup_jobs_by_references(
            7,
            ["sender-1", "sender-2"],
        )
        assert result == rows

    asyncio.run(run())

    statement, parameters = session.execute.await_args.args
    sql = str(statement)
    assert "jobs.provider_id" in sql
    assert "jobs.source_job_id = ANY" in sql
    assert "affiliate_links.job_id = jobs.id" in sql
    assert parameters == {"source_job_ids": ["sender-1", "sender-2"]}
    assert 7 in statement.compile().params.values()


def test_service_lookup_handles_mixed_hits_and_provider_scoping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _AffiliateRepositoryStub(
        [
            _job(1, 7, "provider-seven"),
            _job(2, 8, "shared-reference"),
            _job(3, 7, "another-hit"),
        ]
    )
    monkeypatch.setattr(
        affiliate_service,
        "AffiliateRepository",
        lambda session: repository,
    )

    async def run() -> None:
        result = await affiliate_service.AffiliateService().lookup_jobs(
            object(),  # type: ignore[arg-type]
            7,
            ["provider-seven", "shared-reference", "missing", "another-hit"],
        )
        assert [row["id"] for row in result["matched"]] == [1, 3]
        assert result["not_found"] == ["shared-reference", "missing"]

    asyncio.run(run())
    assert repository.lookup_calls == [
        (
            7,
            ["provider-seven", "shared-reference", "missing", "another-hit"],
        )
    ]


def test_service_lookup_empty_and_nonexistent_references(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _AffiliateRepositoryStub([])
    monkeypatch.setattr(
        affiliate_service,
        "AffiliateRepository",
        lambda session: repository,
    )

    async def run() -> None:
        service = affiliate_service.AffiliateService()
        assert await service.lookup_jobs(object(), 7, []) == {  # type: ignore[arg-type]
            "matched": [],
            "not_found": [],
        }
        assert await service.lookup_jobs(  # type: ignore[arg-type]
            object(),
            7,
            ["does-not-exist"],
        ) == {
            "matched": [],
            "not_found": ["does-not-exist"],
        }

    asyncio.run(run())


def test_repository_lookup_by_ids_revalidates_provider_and_apply_url() -> None:
    session = _session()
    rows = [
        {"id": 1, "apply_url": "https://example.test/apply/1"},
        {"id": 2, "apply_url": None},
    ]
    session.execute.return_value = _result(rows)

    async def run() -> None:
        result = await AffiliateRepository(session).lookup_jobs_by_ids(7, [1, 2, 3])
        assert result == rows

    asyncio.run(run())

    statement = session.execute.await_args.args[0]
    sql = str(statement)
    assert "jobs.provider_id" in sql
    assert "jobs.id IN" in sql
    parameters = statement.compile().params.values()
    assert 7 in parameters
    assert [1, 2, 3] in parameters


def test_generation_revalidation_excludes_missing_apply_url_per_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service_logger = Mock()

    async def lookup_by_ids(
        self: AffiliateRepository,
        provider_id: int,
        job_ids: list[int],
    ) -> list[dict[str, object]]:
        assert provider_id == 7
        assert job_ids == [1, 2, 3]
        return [
            {"id": 1, "apply_url": "https://example.test/apply/1"},
            {"id": 2, "apply_url": None},
        ]

    async def generate_links(
        self: affiliate_service.AffiliateService,
        session: AsyncSession,
        provider_id: int,
        job_ids: list[int],
        admin_id: object = None,
    ) -> list[dict[str, object]]:
        assert provider_id == 7
        assert job_ids == [1]
        assert admin_id is None
        return [{"job_id": 1, "short_hash": "abc123", "redirect_url": "/r/abc123"}]

    monkeypatch.setattr(AffiliateRepository, "lookup_jobs_by_ids", lookup_by_ids)
    monkeypatch.setattr(
        affiliate_service.AffiliateService,
        "generate_links",
        generate_links,
    )
    monkeypatch.setattr(admin_affiliate, "logger", service_logger)

    async def run() -> None:
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/admin/api/affiliate/generate",
            "headers": [],
            "client": ("testclient", 50000),
        }
        response = await admin_affiliate.generate_affiliate_links(
            Request(scope),
            AffiliateGenerateRequest(provider_id=7, job_ids=[1, 2, 3, 2]),
            object(),  # type: ignore[arg-type]
        )
        assert [item.model_dump() for item in response.generated] == [
            {"job_id": 1, "short_hash": "abc123", "redirect_url": "/r/abc123"}
        ]
        assert [item.model_dump() for item in response.excluded] == [
            {"job_id": 2, "reason": "Apply URL is unavailable"},
            {"job_id": 3, "reason": "Job not found for provider"},
        ]

    asyncio.run(run())
    service_logger.warning.assert_called_once_with(
        "Excluded jobs from affiliate-link generation after revalidation",
        extra={"excluded_job_ids": [2, 3]},
    )


def test_repository_create_links_uses_conflict_safe_idempotent_insert() -> None:
    session = _session()
    stored_rows = [
        {
            "id": 101,
            "short_hash": "existing-hash",
            "job_id": 1,
            "provider_id": 7,
            "created_by_admin_id": None,
            "created_at": NOW,
        }
    ]
    session.execute.side_effect = [Mock(), _result(stored_rows)]
    links = [
        {
            "job_id": 1,
            "provider_id": 7,
            "short_hash": "candidate-hash",
        },
        {
            "job_id": 1,
            "provider_id": 7,
            "short_hash": "ignored-duplicate",
        },
    ]

    async def run() -> None:
        result = await AffiliateRepository(session).create_affiliate_links(links)
        assert result == stored_rows

    asyncio.run(run())

    insert_statement = session.execute.await_args_list[0].args[0]
    select_statement = session.execute.await_args_list[1].args[0]
    assert "ON CONFLICT (job_id) DO NOTHING" in str(insert_statement)
    assert "affiliate_links.job_id IN" in str(select_statement)
    assert [1] in select_statement.compile().params.values()


def test_short_hashes_are_unique_and_url_safe() -> None:
    hashes = [
        affiliate_service.AffiliateService.generate_short_hash() for _ in range(100)
    ]

    assert len(set(hashes)) == len(hashes)
    assert all(re.fullmatch(r"[A-Za-z0-9_-]{11}", value) for value in hashes)


def test_generation_deduplicates_jobs_and_repeat_request_reuses_existing_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _AffiliateRepositoryStub([])
    session = _session()
    generated_hashes = iter(
        ["first-hash", "second-hash", "replacement-1", "replacement-2"]
    )
    service = affiliate_service.AffiliateService()
    monkeypatch.setattr(
        affiliate_service,
        "AffiliateRepository",
        lambda current_session: repository,
    )
    monkeypatch.setattr(service, "generate_short_hash", lambda: next(generated_hashes))
    admin_id = uuid4()

    async def run() -> None:
        first = await service.generate_links(session, 7, [2, 1, 2], admin_id)
        repeated = await service.generate_links(session, 7, [1, 2], admin_id)

        assert first == [
            {
                "job_id": 1,
                "short_hash": "second-hash",
                "redirect_url": "/r/second-hash",
            },
            {"job_id": 2, "short_hash": "first-hash", "redirect_url": "/r/first-hash"},
        ]
        assert repeated == first

    asyncio.run(run())

    assert [link["job_id"] for link in repository.create_calls[0]] == [2, 1]
    assert all(
        link["created_by_admin_id"] == admin_id
        for call_links in repository.create_calls
        for link in call_links
    )
    assert session.commit.await_count == 2
    session.rollback.assert_not_awaited()


def test_generate_empty_input_does_not_open_repository_or_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository_factory = Mock(side_effect=AssertionError("repository opened"))
    monkeypatch.setattr(affiliate_service, "AffiliateRepository", repository_factory)
    session = _session()

    async def run() -> None:
        assert (
            await affiliate_service.AffiliateService().generate_links(
                session,
                7,
                [],
            )
            == []
        )

    asyncio.run(run())
    repository_factory.assert_not_called()
    session.commit.assert_not_awaited()
    session.rollback.assert_not_awaited()


def test_short_hash_collision_rolls_back_and_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collision = _integrity_error(
        constraint_name=affiliate_service.SHORT_HASH_CONSTRAINT
    )
    repository = SimpleNamespace(
        create_affiliate_links=AsyncMock(
            side_effect=[
                collision,
                [
                    {
                        "job_id": 1,
                        "short_hash": "second-hash",
                    }
                ],
            ]
        )
    )
    session = _session()
    service = affiliate_service.AffiliateService()
    generate_hash = Mock(side_effect=["first-hash", "second-hash"])
    monkeypatch.setattr(service, "generate_short_hash", generate_hash)
    monkeypatch.setattr(
        affiliate_service,
        "AffiliateRepository",
        lambda current_session: repository,
    )

    async def run() -> None:
        assert await service.generate_links(session, 7, [1]) == [
            {
                "job_id": 1,
                "short_hash": "second-hash",
                "redirect_url": "/r/second-hash",
            }
        ]

    asyncio.run(run())
    assert repository.create_affiliate_links.await_count == 2
    session.rollback.assert_awaited_once_with()
    session.commit.assert_awaited_once_with()


def test_non_collision_integrity_error_rolls_back_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_error = _integrity_error(constraint_name="uq_affiliate_links_job_id")
    repository = SimpleNamespace(
        create_affiliate_links=AsyncMock(side_effect=database_error)
    )
    session = _session()
    monkeypatch.setattr(
        affiliate_service,
        "AffiliateRepository",
        lambda current_session: repository,
    )

    async def run() -> None:
        with pytest.raises(IntegrityError) as exc_info:
            await affiliate_service.AffiliateService().generate_links(
                session,
                7,
                [1],
            )
        assert exc_info.value is database_error

    asyncio.run(run())
    repository.create_affiliate_links.assert_awaited_once()
    session.rollback.assert_awaited_once_with()
    session.commit.assert_not_awaited()


def test_collision_retry_exhaustion_reraises_last_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collision = _integrity_error(message=affiliate_service.SHORT_HASH_CONSTRAINT)
    repository = SimpleNamespace(
        create_affiliate_links=AsyncMock(side_effect=collision)
    )
    session = _session()
    monkeypatch.setattr(
        affiliate_service,
        "AffiliateRepository",
        lambda current_session: repository,
    )

    async def run() -> None:
        with pytest.raises(IntegrityError) as exc_info:
            await affiliate_service.AffiliateService().generate_links(
                session,
                7,
                [1],
            )
        assert exc_info.value is collision

    asyncio.run(run())
    assert repository.create_affiliate_links.await_count == 3
    assert session.rollback.await_count == 3
    session.commit.assert_not_awaited()


def test_database_error_rolls_back_and_reraises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_error = OperationalError(
        "INSERT",
        {},
        RuntimeError("database unavailable"),
    )
    repository = SimpleNamespace(
        create_affiliate_links=AsyncMock(side_effect=database_error)
    )
    session = _session()
    monkeypatch.setattr(
        affiliate_service,
        "AffiliateRepository",
        lambda current_session: repository,
    )

    async def run() -> None:
        with pytest.raises(OperationalError) as exc_info:
            await affiliate_service.AffiliateService().generate_links(
                session,
                7,
                [1],
            )
        assert exc_info.value is database_error

    asyncio.run(run())
    session.rollback.assert_awaited_once_with()
    session.commit.assert_not_awaited()


def test_repository_database_error_propagates() -> None:
    database_error = OperationalError(
        "SELECT",
        {},
        RuntimeError("database unavailable"),
    )
    session = _session()
    session.execute.side_effect = database_error

    async def run() -> None:
        with pytest.raises(OperationalError) as exc_info:
            await AffiliateRepository(session).lookup_jobs_by_references(
                7,
                ["sender-1"],
            )
        assert exc_info.value is database_error

    asyncio.run(run())


def test_short_hash_collision_detects_wrapped_driver_constraint() -> None:
    driver_error = Exception("driver error")
    driver_error.constraint_name = (  # type: ignore[attr-defined]
        affiliate_service.SHORT_HASH_CONSTRAINT
    )
    wrapper = Exception("wrapped")
    wrapper.__cause__ = driver_error
    error = IntegrityError("INSERT", {}, wrapper)

    assert affiliate_service.AffiliateService._is_short_hash_collision(error) is True


def test_zero_retry_attempts_hits_defensive_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(affiliate_service, "MAX_SHORT_HASH_ATTEMPTS", 0)
    monkeypatch.setattr(
        affiliate_service,
        "AffiliateRepository",
        lambda current_session: SimpleNamespace(),
    )

    async def run() -> None:
        with pytest.raises(RuntimeError, match="retry loop exited unexpectedly"):
            await affiliate_service.AffiliateService().generate_links(
                _session(),
                7,
                [1],
            )

    asyncio.run(run())


def test_repository_get_by_short_hash_returns_redirect_state_or_none() -> None:
    session = _session()
    found_row = {
        "short_hash": "abc123",
        "apply_url": "https://example.test/apply/1",
        "is_active": False,
        "slug": "expired-job",
    }
    found_mappings = Mock()
    found_mappings.one_or_none.return_value = found_row
    found_result = Mock()
    found_result.mappings.return_value = found_mappings
    missing_mappings = Mock()
    missing_mappings.one_or_none.return_value = None
    missing_result = Mock()
    missing_result.mappings.return_value = missing_mappings
    session.execute.side_effect = [found_result, missing_result]

    async def run() -> None:
        repository = AffiliateRepository(session)
        assert await repository.get_by_short_hash("abc123") == found_row
        assert await repository.get_by_short_hash("missing") is None

    asyncio.run(run())

    statement = session.execute.await_args_list[0].args[0]
    sql = str(statement)
    assert "JOIN jobs ON jobs.id = affiliate_links.job_id" in sql
    assert "affiliate_links.short_hash" in sql
    assert "abc123" in statement.compile().params.values()
