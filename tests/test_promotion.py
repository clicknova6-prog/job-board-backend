"""Tests for promotion anomaly outcomes."""

from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from sqlalchemy.exc import OperationalError

from app.db.models import ImportRun
from app.db.repositories import PromotionRepository as DatabasePromotionRepository
from app.imports import promotion
from app.imports.exceptions import TransientImportError


class _PromotionRepositoryStub:
    """Provide promotion inputs while reusing the real outcome mutation logic."""

    def __init__(
        self,
        run: ImportRun,
        *,
        active_count: int,
        valid_staged_count: int,
    ) -> None:
        self.run = run
        self.active_count = active_count
        self.staged_rows = [object() for _ in range(valid_staged_count)]
        self.commit_count = 0
        self.rollback_count = 0
        self.deactivate_calls: list[tuple[int, int, object]] = []

    def get_import_run(self, import_run_id: int) -> ImportRun | None:
        return self.run if import_run_id == self.run.id else None

    def get_provider(self, provider_id: int) -> SimpleNamespace | None:
        if provider_id != self.run.provider_id:
            return None
        return SimpleNamespace(config={})

    def load_valid_staged_jobs(self, import_run_id: int) -> list[object]:
        assert import_run_id == self.run.id
        return self.staged_rows

    def count_active_jobs(self, provider_id: int) -> int:
        assert provider_id == self.run.provider_id
        return self.active_count

    def deactivate_stale_jobs(
        self,
        provider_id: int,
        run_id: int,
        now: object,
    ) -> int:
        assert provider_id == self.run.provider_id
        assert run_id == self.run.id
        self.deactivate_calls.append((provider_id, run_id, now))
        return 0

    def finish_promotion(self, run: ImportRun, **kwargs: Any) -> None:
        DatabasePromotionRepository.finish_promotion(self, run, **kwargs)

    def commit(self) -> None:
        self.commit_count += 1

    def rollback(self) -> None:
        self.rollback_count += 1


class _FullPromotionRepositoryStub:
    """Model promotion writes and make transaction boundaries observable."""

    def __init__(
        self,
        run: ImportRun | None,
        *,
        staged_rows: list[SimpleNamespace] | None = None,
        active_count: int = 0,
        provider_config: dict[str, Any] | None = None,
        provider_exists: bool = True,
        deactivated_jobs: int = 0,
        existing_jobs: dict[str, SimpleNamespace] | None = None,
    ) -> None:
        self.run = run
        self.staged_rows = staged_rows or []
        self.active_count = active_count
        self.provider_config = provider_config
        self.provider_exists = provider_exists
        self.deactivated_jobs = deactivated_jobs
        self.jobs_by_source = dict(existing_jobs or {})
        self.created_jobs: list[tuple[SimpleNamespace, SimpleNamespace, str]] = []
        self.updated_jobs: list[tuple[SimpleNamespace, SimpleNamespace, int]] = []
        self.seen_jobs: list[tuple[SimpleNamespace, int]] = []
        self.deactivate_calls: list[tuple[int, int, object]] = []
        self.finished_promotions: list[dict[str, Any]] = []
        self.flush_count = 0
        self.commit_count = 0
        self.rollback_count = 0

    def get_import_run(self, import_run_id: int) -> ImportRun | None:
        if self.run is None or import_run_id != self.run.id:
            return None
        return self.run

    def get_provider(self, provider_id: int) -> SimpleNamespace | None:
        if (
            not self.provider_exists
            or self.run is None
            or provider_id != self.run.provider_id
        ):
            return None
        return SimpleNamespace(config=self.provider_config)

    def load_valid_staged_jobs(self, import_run_id: int) -> list[SimpleNamespace]:
        assert self.run is not None
        assert import_run_id == self.run.id
        return self.staged_rows

    def count_active_jobs(self, provider_id: int) -> int:
        assert self.run is not None
        assert provider_id == self.run.provider_id
        return self.active_count

    def get_job_by_provider_and_source(
        self,
        provider_id: int,
        source_job_id: str,
    ) -> SimpleNamespace | None:
        assert self.run is not None
        assert provider_id == self.run.provider_id
        return self.jobs_by_source.get(source_job_id)

    def create_job(
        self,
        *,
        run: ImportRun,
        staged: SimpleNamespace,
        placeholder_slug: str,
        now: object,
    ) -> SimpleNamespace:
        job = SimpleNamespace(
            id=1000 + len(self.created_jobs),
            slug=placeholder_slug,
            payload_hash=staged.payload_hash,
        )
        self.created_jobs.append((job, staged, placeholder_slug))
        self.jobs_by_source[staged.source_job_id] = job
        return job

    def update_job_from_staged(
        self,
        existing: SimpleNamespace,
        staged: SimpleNamespace,
        run_id: int,
        now: object,
    ) -> None:
        existing.payload_hash = staged.payload_hash
        self.updated_jobs.append((existing, staged, run_id))

    def mark_job_seen(
        self,
        existing: SimpleNamespace,
        run_id: int,
        now: object,
    ) -> None:
        self.seen_jobs.append((existing, run_id))

    def flush(self) -> None:
        self.flush_count += 1

    def deactivate_stale_jobs(
        self,
        provider_id: int,
        run_id: int,
        now: object,
    ) -> int:
        self.deactivate_calls.append((provider_id, run_id, now))
        return self.deactivated_jobs

    def finish_promotion(self, run: ImportRun, **kwargs: Any) -> None:
        self.finished_promotions.append({"run": run, **kwargs})
        DatabasePromotionRepository.finish_promotion(self, run, **kwargs)

    def commit(self) -> None:
        self.commit_count += 1

    def rollback(self) -> None:
        self.rollback_count += 1


def _import_run(
    *,
    run_id: int = 1,
    provider_id: int = 1,
    records_received: int = 0,
    records_rejected: int = 0,
) -> ImportRun:
    return ImportRun(
        id=run_id,
        provider_id=provider_id,
        source_name="jobg8",
        status="processing",
        records_received=records_received,
        records_staged=records_received - records_rejected,
        records_imported=0,
        records_rejected=records_rejected,
        new_jobs=0,
        updated_jobs=0,
        deleted_jobs=0,
        is_anomalous=False,
        anomaly_reasons=[],
    )


def _staged_job(
    source_job_id: str,
    *,
    payload_hash: str,
    title: str | None = "Software Engineer",
    advertiser_name: str | None = "Acme, Inc.",
) -> SimpleNamespace:
    return SimpleNamespace(
        source_job_id=source_job_id,
        payload_hash=payload_hash,
        title=title,
        advertiser_name=advertiser_name,
    )


def _configure_repository(
    monkeypatch: pytest.MonkeyPatch,
    repository: _FullPromotionRepositoryStub,
    *,
    filters_cache_invalidator: Mock | None = None,
) -> None:
    monkeypatch.setattr(promotion, "SessionLocal", lambda: nullcontext(object()))
    monkeypatch.setattr(
        promotion,
        "PromotionRepository",
        lambda session: repository,
    )
    monkeypatch.setattr(
        promotion,
        "filters_cache_invalidator",
        filters_cache_invalidator or Mock(),
    )


def _run_promotion(
    monkeypatch: pytest.MonkeyPatch,
    *,
    active_count: int,
    valid_staged_count: int,
    records_received: int,
    records_rejected: int,
    initially_anomalous: bool = False,
) -> ImportRun:
    run = ImportRun(
        id=1,
        provider_id=1,
        source_name="jobg8",
        status="processing",
        records_received=records_received,
        records_staged=valid_staged_count,
        records_imported=0,
        records_rejected=records_rejected,
        new_jobs=0,
        updated_jobs=0,
        deleted_jobs=0,
        is_anomalous=initially_anomalous,
        anomaly_reasons=["catalogue_drop"] if initially_anomalous else [],
    )
    repository = _PromotionRepositoryStub(
        run,
        active_count=active_count,
        valid_staged_count=valid_staged_count,
    )
    monkeypatch.setattr(promotion, "SessionLocal", lambda: nullcontext(object()))
    monkeypatch.setattr(promotion, "PromotionRepository", lambda session: repository)

    promotion.PromotionService(run.id).run()
    return run


def test_catalogue_drop_sets_machine_readable_anomaly_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run_promotion(
        monkeypatch,
        active_count=100,
        valid_staged_count=1,
        records_received=1,
        records_rejected=0,
    )

    assert run.is_anomalous is True
    assert run.anomaly_reasons == ["catalogue_drop"]


def test_high_rejection_rate_sets_machine_readable_anomaly_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run_promotion(
        monkeypatch,
        active_count=0,
        valid_staged_count=1,
        records_received=100,
        records_rejected=50,
    )

    assert run.is_anomalous is True
    assert run.anomaly_reasons == ["high_rejection_rate"]


def test_normal_promotion_clears_anomaly_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run_promotion(
        monkeypatch,
        active_count=0,
        valid_staged_count=0,
        records_received=10,
        records_rejected=0,
        initially_anomalous=True,
    )

    assert run.is_anomalous is False
    assert run.anomaly_reasons == []


@pytest.mark.parametrize(
    (
        "active_count",
        "valid_staged_count",
        "drop_threshold_pct",
        "records_received",
        "records_rejected",
        "rejection_rate_pct",
        "expected_codes",
    ),
    [
        (100, 79, 20, 100, 0, 15, ["catalogue_drop"]),
        (100, 80, 20, 100, 0, 15, []),
        (0, 0, 20, 100, 16, 15, ["high_rejection_rate"]),
        (0, 0, 20, 100, 15, 15, []),
        (0, 0, 20, 0, 0, 15, []),
        (100, 79, 20, 100, 16, 15, ["catalogue_drop", "high_rejection_rate"]),
    ],
)
def test_anomaly_threshold_boundaries(
    active_count: int,
    valid_staged_count: int,
    drop_threshold_pct: float,
    records_received: int,
    records_rejected: int,
    rejection_rate_pct: float,
    expected_codes: list[str],
) -> None:
    reasons, reason_codes = promotion.PromotionService._anomaly_reasons(
        active_count=active_count,
        valid_staged_count=valid_staged_count,
        drop_threshold_pct=drop_threshold_pct,
        rejection_rate_pct=rejection_rate_pct,
        records_received=records_received,
        records_rejected=records_rejected,
    )

    assert reason_codes == expected_codes
    assert len(reasons) == len(expected_codes)


def test_anomalous_feed_preserves_live_jobs_and_only_flags_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _import_run(records_received=10)
    repository = _FullPromotionRepositoryStub(
        run,
        staged_rows=[_staged_job("job-1", payload_hash="new")],
        active_count=10,
        existing_jobs={"job-1": SimpleNamespace(payload_hash="old")},
    )
    service_logger = Mock()
    cache_invalidator = Mock()
    _configure_repository(
        monkeypatch, repository, filters_cache_invalidator=cache_invalidator
    )

    summary = promotion.PromotionService(
        run.id,
        service_logger=service_logger,
    ).run()

    assert summary == promotion.PromotionSummary(0, 0, 0, 0)
    assert repository.created_jobs == []
    assert repository.updated_jobs == []
    assert repository.seen_jobs == []
    assert repository.deactivate_calls == []
    assert repository.flush_count == 0
    assert repository.commit_count == 1
    assert run.status == "failed"
    assert run.is_anomalous is True
    assert run.anomaly_reasons == ["catalogue_drop"]
    service_logger.error.assert_called_once()
    # An aborted promotion never touches the live jobs table, so the filters
    # cache is left alone too -- nothing to invalidate.
    cache_invalidator.bump_version.assert_not_called()


def test_normal_feed_promotes_all_outcomes_in_separate_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _import_run(records_received=3)
    changed_job = SimpleNamespace(payload_hash="old-hash")
    unchanged_job = SimpleNamespace(payload_hash="same-hash")
    repository = _FullPromotionRepositoryStub(
        run,
        staged_rows=[
            _staged_job("new-job", payload_hash="new-hash"),
            _staged_job("changed-job", payload_hash="changed-hash"),
            _staged_job("same-job", payload_hash="same-hash"),
        ],
        active_count=0,
        deactivated_jobs=2,
        existing_jobs={
            "changed-job": changed_job,
            "same-job": unchanged_job,
        },
    )
    service_logger = Mock()
    cache_invalidator = Mock()
    _configure_repository(
        monkeypatch, repository, filters_cache_invalidator=cache_invalidator
    )

    summary = promotion.PromotionService(
        run.id,
        batch_size=2,
        service_logger=service_logger,
    ).run()

    assert summary == promotion.PromotionSummary(
        new_jobs=1,
        updated_jobs=1,
        unchanged_jobs=1,
        deactivated_jobs=2,
    )
    assert len(repository.created_jobs) == 1
    created_job, staged, placeholder = repository.created_jobs[0]
    assert placeholder == "__pending__1__new-job"
    assert created_job.slug == "software-engineer-acme-inc-1000"
    assert staged.source_job_id == "new-job"
    assert repository.flush_count == 1
    assert repository.updated_jobs[0][1].source_job_id == "changed-job"
    assert repository.seen_jobs == [(unchanged_job, run.id)]
    assert len(repository.deactivate_calls) == 1
    # Two batch commits plus the final outcome/deactivation commit.
    assert repository.commit_count == 3
    assert service_logger.info.call_count == 2
    assert run.status == "completed"
    assert run.records_imported == 2
    assert run.new_jobs == 1
    assert run.updated_jobs == 1
    assert run.deleted_jobs == 2
    assert run.is_anomalous is False
    # A completed promotion invalidates the filters cache exactly once, after
    # its final commit -- never before the outcome is durable.
    cache_invalidator.bump_version.assert_called_once_with()


def test_duplicate_staged_identity_does_not_create_two_canonical_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _import_run(records_received=2)
    duplicate_rows = [
        _staged_job("duplicate", payload_hash="same-hash"),
        _staged_job("duplicate", payload_hash="same-hash"),
    ]
    repository = _FullPromotionRepositoryStub(run, staged_rows=duplicate_rows)
    _configure_repository(monkeypatch, repository)

    summary = promotion.PromotionService(run.id, batch_size=2).run()

    assert summary.new_jobs == 1
    assert summary.updated_jobs == 0
    assert summary.unchanged_jobs == 1
    assert len(repository.created_jobs) == 1
    assert len(repository.seen_jobs) == 1


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Senior Software Engineer — Acme, Inc.", "senior-software-engineer-acme-inc"),
        ("---Already Slug-Like---", "already-slug-like"),
        ("***", ""),
    ],
)
def test_slugify(value: str, expected: str) -> None:
    assert promotion.slugify(value) == expected


def test_invalid_batch_size_is_rejected() -> None:
    with pytest.raises(ValueError, match="batch_size must be greater than zero"):
        promotion.PromotionService(1, batch_size=0)


def test_missing_import_run_rolls_back_and_reraises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _FullPromotionRepositoryStub(None)
    _configure_repository(monkeypatch, repository)
    service = promotion.PromotionService(404)
    mark_failed = Mock()
    monkeypatch.setattr(service, "_mark_failed", mark_failed)

    with pytest.raises(ValueError, match="ImportRun 404 does not exist") as exc_info:
        service.run()

    assert repository.rollback_count == 1
    mark_failed.assert_called_once_with(exc_info.value)


def test_missing_provider_rolls_back_and_reraises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _import_run(provider_id=404)
    repository = _FullPromotionRepositoryStub(run, provider_exists=False)
    _configure_repository(monkeypatch, repository)
    service = promotion.PromotionService(run.id)
    mark_failed = Mock()
    monkeypatch.setattr(service, "_mark_failed", mark_failed)

    with pytest.raises(ValueError, match="Provider 404 does not exist") as exc_info:
        service.run()

    assert repository.rollback_count == 1
    mark_failed.assert_called_once_with(exc_info.value)


def test_operational_error_rolls_back_marks_failed_and_becomes_transient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _import_run()
    repository = _FullPromotionRepositoryStub(run)
    database_error = OperationalError("SELECT", {}, RuntimeError("connection lost"))
    repository.load_valid_staged_jobs = Mock(side_effect=database_error)
    _configure_repository(monkeypatch, repository)
    service = promotion.PromotionService(run.id)
    mark_failed = Mock()
    monkeypatch.setattr(service, "_mark_failed", mark_failed)

    with pytest.raises(TransientImportError) as exc_info:
        service.run()

    assert exc_info.value.import_run_id == run.id
    assert exc_info.value.__cause__ is database_error
    assert repository.rollback_count == 1
    mark_failed.assert_called_once_with(database_error)


def test_mark_failed_persists_current_promotion_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _import_run()
    run.records_imported = 8
    run.new_jobs = 3
    run.updated_jobs = 5
    run.deleted_jobs = 2
    repository = _FullPromotionRepositoryStub(run)
    _configure_repository(monkeypatch, repository)
    service = promotion.PromotionService(run.id)

    service._mark_failed(RuntimeError("promotion failed"))

    assert repository.finished_promotions == [
        {
            "run": run,
            "status": "failed",
            "records_imported": 8,
            "new_jobs": 3,
            "updated_jobs": 5,
            "deleted_jobs": 2,
            "error_message": "promotion failed",
        }
    ]
    assert repository.commit_count == 1


def test_mark_failed_logs_when_import_run_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _FullPromotionRepositoryStub(None)
    service_logger = Mock()
    _configure_repository(monkeypatch, repository)
    service = promotion.PromotionService(404, service_logger=service_logger)

    service._mark_failed(RuntimeError("promotion failed"))

    service_logger.error.assert_called_once_with(
        "Cannot mark missing import run as failed",
        extra={"import_run_id": 404},
    )
    assert repository.commit_count == 0


def test_mark_failed_logs_when_failure_outcome_cannot_be_persisted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service_logger = Mock()

    def fail_to_open_session() -> None:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(promotion, "SessionLocal", fail_to_open_session)
    service = promotion.PromotionService(1, service_logger=service_logger)

    service._mark_failed(RuntimeError("promotion failed"))

    service_logger.exception.assert_called_once_with(
        "Could not persist failed promotion outcome",
        extra={"import_run_id": 1},
    )
