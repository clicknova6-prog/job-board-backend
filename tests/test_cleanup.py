"""Unit tests for expired soft-deleted job cleanup orchestration."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, call

import pytest
from sqlalchemy.exc import OperationalError

from app.db.repositories import ProviderRetentionPolicy
from app.imports import cleanup

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class _JobState:
    id: int
    provider_id: int
    is_active: bool
    deactivated_at: datetime | None


class _CleanupRepositoryStub:
    """Model cleanup selection and transaction boundaries in memory."""

    def __init__(
        self,
        policies: list[ProviderRetentionPolicy],
        jobs: list[_JobState],
        *,
        list_error: Exception | None = None,
        commit_error: Exception | None = None,
    ) -> None:
        self.policies = policies
        self.jobs = {job.id: job for job in jobs}
        self.list_error = list_error
        self.commit_error = commit_error
        self.pending_deletions: list[int] = []
        self.find_calls: list[dict[str, object]] = []
        self.delete_calls: list[dict[str, object]] = []
        self.commit_attempts = 0
        self.commit_count = 0
        self.rollback_count = 0

    def list_provider_retention_policies(self) -> list[ProviderRetentionPolicy]:
        if self.list_error is not None:
            raise self.list_error
        return self.policies

    def find_expired_job_ids(
        self,
        *,
        provider_id: int,
        cutoff: datetime,
        limit: int,
    ) -> list[int]:
        self.find_calls.append(
            {
                "provider_id": provider_id,
                "cutoff": cutoff,
                "limit": limit,
            }
        )
        return sorted(
            job.id
            for job in self.jobs.values()
            if job.provider_id == provider_id
            and not job.is_active
            and job.deactivated_at is not None
            and job.deactivated_at < cutoff
        )[:limit]

    def hard_delete_expired_jobs(
        self,
        *,
        job_ids: list[int],
        provider_id: int,
        cutoff: datetime,
    ) -> int:
        self.delete_calls.append(
            {
                "job_ids": list(job_ids),
                "provider_id": provider_id,
                "cutoff": cutoff,
            }
        )
        eligible_ids = [
            job_id
            for job_id in job_ids
            if (job := self.jobs.get(job_id)) is not None
            and job.provider_id == provider_id
            and not job.is_active
            and job.deactivated_at is not None
            and job.deactivated_at < cutoff
        ]
        self.pending_deletions.extend(eligible_ids)
        return len(eligible_ids)

    def commit(self) -> None:
        self.commit_attempts += 1
        if self.commit_error is not None:
            raise self.commit_error
        for job_id in self.pending_deletions:
            self.jobs.pop(job_id, None)
        self.pending_deletions.clear()
        self.commit_count += 1

    def rollback(self) -> None:
        self.pending_deletions.clear()
        self.rollback_count += 1


class _FixedDateTime(datetime):
    @classmethod
    def now(cls, tz: object = None) -> datetime:
        assert tz is UTC
        return NOW


def _policy(
    *,
    provider_id: int = 7,
    provider_name: str = "jobg8",
    retention_hours: int | None = None,
) -> ProviderRetentionPolicy:
    return ProviderRetentionPolicy(
        provider_id=provider_id,
        provider_name=provider_name,
        retention_hours=retention_hours,
    )


def _job(
    job_id: int,
    *,
    age: timedelta | None,
    provider_id: int = 7,
    is_active: bool = False,
) -> _JobState:
    return _JobState(
        id=job_id,
        provider_id=provider_id,
        is_active=is_active,
        deactivated_at=None if age is None else NOW - age,
    )


def _configure_repository(
    monkeypatch: pytest.MonkeyPatch,
    repository: _CleanupRepositoryStub | Mock,
) -> None:
    monkeypatch.setattr(cleanup, "datetime", _FixedDateTime)
    monkeypatch.setattr(cleanup, "SessionLocal", lambda: nullcontext(object()))
    monkeypatch.setattr(cleanup, "CleanupRepository", lambda session: repository)


def test_default_retention_deletes_only_inactive_jobs_older_than_twelve_hours(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jobs = [
        _job(1, age=timedelta(hours=12, seconds=1)),
        _job(2, age=timedelta(hours=11, minutes=59, seconds=59)),
        _job(3, age=timedelta(hours=12)),
        _job(4, age=timedelta(days=2), is_active=True),
        _job(5, age=None),
        _job(6, age=timedelta(days=2), provider_id=8),
    ]
    repository = _CleanupRepositoryStub([_policy()], jobs)
    service_logger = Mock()
    _configure_repository(monkeypatch, repository)

    summary = cleanup.CleanupService(service_logger=service_logger).run()

    assert summary == cleanup.CleanupSummary(
        checked_jobs=1,
        deleted_jobs=1,
        providers=[
            cleanup.ProviderCleanupSummary(
                provider_id=7,
                provider_name="jobg8",
                retention_hours=12,
                checked_jobs=1,
                deleted_jobs=1,
            )
        ],
    )
    assert set(repository.jobs) == {2, 3, 4, 5, 6}
    expected_cutoff = NOW - timedelta(hours=12)
    assert repository.find_calls == [
        {"provider_id": 7, "cutoff": expected_cutoff, "limit": 1000},
        {"provider_id": 7, "cutoff": expected_cutoff, "limit": 1000},
    ]
    assert service_logger.info.call_args_list == [
        call(
            "Expired job cleanup batch committed",
            extra={
                "provider_id": 7,
                "provider_name": "jobg8",
                "checked_jobs": 1,
                "deleted_jobs": 1,
            },
        ),
        call(
            "Expired job cleanup completed for provider",
            extra={
                "provider_id": 7,
                "provider_name": "jobg8",
                "retention_hours": 12,
                "checked_jobs": 1,
                "deleted_jobs": 1,
            },
        ),
        call(
            "Expired job cleanup completed",
            extra={
                "checked_jobs": 1,
                "deleted_jobs": 1,
                "provider_count": 1,
            },
        ),
    ]


def test_configured_retention_controls_cutoff_and_boundary_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retention_hours = 6
    repository = _CleanupRepositoryStub(
        [_policy(retention_hours=retention_hours)],
        [
            _job(1, age=timedelta(hours=retention_hours, seconds=1)),
            _job(
                2,
                age=timedelta(
                    hours=retention_hours - 1,
                    minutes=59,
                    seconds=59,
                ),
            ),
            _job(3, age=timedelta(hours=retention_hours)),
        ],
    )
    _configure_repository(monkeypatch, repository)

    summary = cleanup.CleanupService().run()

    assert summary.providers[0].retention_hours == retention_hours
    assert summary.deleted_jobs == 1
    assert set(repository.jobs) == {2, 3}
    assert {
        item["cutoff"] for item in repository.find_calls
    } == {NOW - timedelta(hours=retention_hours)}


def test_expired_jobs_are_processed_in_ordered_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _CleanupRepositoryStub(
        [_policy(retention_hours=24)],
        [_job(job_id, age=timedelta(hours=25)) for job_id in range(1, 6)],
    )
    service_logger = Mock()
    _configure_repository(monkeypatch, repository)

    summary = cleanup.CleanupService(
        batch_size=2,
        service_logger=service_logger,
    ).run()

    assert summary.checked_jobs == 5
    assert summary.deleted_jobs == 5
    assert repository.jobs == {}
    assert [item["job_ids"] for item in repository.delete_calls] == [
        [1, 2],
        [3, 4],
        [5],
    ]
    assert repository.commit_count == 3
    assert len(repository.find_calls) == 4
    assert service_logger.info.call_count == 5


def test_provider_with_no_expired_jobs_completes_without_committing_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _CleanupRepositoryStub(
        [_policy(retention_hours=24)],
        [_job(1, age=timedelta(hours=1))],
    )
    service_logger = Mock()
    _configure_repository(monkeypatch, repository)

    summary = cleanup.CleanupService(service_logger=service_logger).run()

    assert summary == cleanup.CleanupSummary(
        checked_jobs=0,
        deleted_jobs=0,
        providers=[
            cleanup.ProviderCleanupSummary(7, "jobg8", 24, 0, 0),
        ],
    )
    assert repository.delete_calls == []
    assert repository.commit_count == 0
    assert service_logger.info.call_count == 2


def test_no_provider_policies_returns_empty_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _CleanupRepositoryStub([], [])
    service_logger = Mock()
    _configure_repository(monkeypatch, repository)

    summary = cleanup.CleanupService(service_logger=service_logger).run()

    assert summary == cleanup.CleanupSummary(0, 0, [])
    service_logger.info.assert_called_once_with(
        "Expired job cleanup completed",
        extra={"checked_jobs": 0, "deleted_jobs": 0, "provider_count": 0},
    )


def test_policy_lookup_database_error_rolls_back_and_reraises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_error = OperationalError(
        "SELECT providers",
        {},
        RuntimeError("database unavailable"),
    )
    repository = _CleanupRepositoryStub([], [], list_error=database_error)
    _configure_repository(monkeypatch, repository)

    with pytest.raises(OperationalError) as exc_info:
        cleanup.CleanupService().run()

    assert exc_info.value is database_error
    assert repository.rollback_count == 1
    assert repository.commit_count == 0


def test_commit_database_error_rolls_back_uncommitted_deletions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_error = OperationalError(
        "COMMIT",
        {},
        RuntimeError("connection lost"),
    )
    repository = _CleanupRepositoryStub(
        [_policy()],
        [_job(1, age=timedelta(hours=13))],
        commit_error=database_error,
    )
    service_logger = Mock()
    _configure_repository(monkeypatch, repository)

    with pytest.raises(OperationalError) as exc_info:
        cleanup.CleanupService(service_logger=service_logger).run()

    assert exc_info.value is database_error
    assert set(repository.jobs) == {1}
    assert repository.pending_deletions == []
    assert repository.commit_attempts == 1
    assert repository.commit_count == 0
    assert repository.rollback_count == 1
    service_logger.info.assert_not_called()


@pytest.mark.parametrize("batch_size", [0, -1])
def test_invalid_batch_size_is_rejected(batch_size: int) -> None:
    with pytest.raises(ValueError, match="batch_size must be greater than zero"):
        cleanup.CleanupService(batch_size=batch_size)
