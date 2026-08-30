"""Unit tests for the feed import orchestration service."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import OperationalError

from app.imports import importer
from app.imports.exceptions import TransientImportError
from app.imports.hashing import compute_payload_hash
from app.imports.schemas import JobFeedRecord


def _valid_raw_record(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "SenderReference": "job-123",
        "Position": "Senior Remote Software Engineer",
        "Description": "Build software for a distributed team.",
        "ApplicationURL": "https://example.com/jobs/123",
    }
    record.update(overrides)
    return record


def _import_run(*, run_id: int = 17, provider_id: int = 42) -> SimpleNamespace:
    return SimpleNamespace(
        id=run_id,
        provider_id=provider_id,
        records_received=9,
        records_staged=8,
        records_imported=7,
        records_rejected=1,
    )


class _JobRepositoryStub:
    def __init__(self, *, new_run_id: int = 101) -> None:
        self.new_run_id = new_run_id
        self.created_runs: list[dict[str, Any]] = []
        self.staged_jobs: list[dict[str, Any]] = []
        self.unmapped_counts: list[dict[str, int]] = []
        self.fallback_counts: list[dict[str, int]] = []
        self.finished_imports: list[dict[str, Any]] = []
        self.flush_count = 0
        self.commit_count = 0

    def create_import_run(self, **kwargs: Any) -> SimpleNamespace:
        self.created_runs.append(kwargs)
        return SimpleNamespace(id=self.new_run_id)

    def flush(self) -> None:
        self.flush_count += 1

    def stage_job(self, **kwargs: Any) -> None:
        self.staged_jobs.append(kwargs)

    def record_unmapped_fields(
        self,
        *,
        run: SimpleNamespace,
        counts: dict[str, int],
    ) -> None:
        assert run.id > 0
        self.unmapped_counts.append(dict(counts))

    def record_field_fallback_warnings(
        self,
        *,
        run: SimpleNamespace,
        counts: dict[str, int],
    ) -> None:
        assert run.id > 0
        self.fallback_counts.append(dict(counts))

    def finish_import(self, **kwargs: Any) -> None:
        self.finished_imports.append(kwargs)

    def commit(self) -> None:
        self.commit_count += 1


class _LookupRepositoryStub:
    def __init__(
        self,
        *,
        run: SimpleNamespace | None = None,
        provider: SimpleNamespace | None = None,
    ) -> None:
        self.run = run
        self.provider = provider
        self.run_lookups: list[int] = []
        self.provider_lookups: list[int] = []

    def get_import_run(self, import_run_id: int) -> SimpleNamespace | None:
        self.run_lookups.append(import_run_id)
        return self.run

    def get_provider(self, provider_id: int) -> SimpleNamespace | None:
        self.provider_lookups.append(provider_id)
        return self.provider


def _parser_for(
    raw_records: list[dict[str, Any]],
) -> Callable[..., Iterator[JobFeedRecord]]:
    def parse_records(
        source: Path,
        *,
        on_validation_error: Callable[
            [dict[str, str | None], ValidationError], None
        ],
    ) -> Iterator[JobFeedRecord]:
        assert isinstance(source, Path)
        for raw_record in raw_records:
            try:
                yield JobFeedRecord.model_validate(raw_record)
            except ValidationError as error:
                on_validation_error(raw_record, error)

    return parse_records


def _configure_run_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    *,
    run: SimpleNamespace | None,
    repository: _JobRepositoryStub,
    raw_records: list[dict[str, Any]],
) -> _LookupRepositoryStub:
    lookup_repository = _LookupRepositoryStub(run=run)
    monkeypatch.setattr(importer, "SessionLocal", lambda: nullcontext(object()))
    monkeypatch.setattr(
        importer,
        "PromotionRepository",
        lambda session: lookup_repository,
    )
    monkeypatch.setattr(importer, "JobRepository", lambda session: repository)
    monkeypatch.setattr(importer, "parse_job_feed", _parser_for(raw_records))
    return lookup_repository


def test_import_service_initialization_and_invalid_progress_interval() -> None:
    service_logger = Mock()
    service = importer.ImportService(
        "feed.xml",
        provider_id=42,
        import_run_id=17,
        progress_interval=5,
        service_logger=service_logger,
    )

    assert service.feed_path == Path("feed.xml")
    assert service.provider_id == 42
    assert service.import_run_id == 17
    assert service.progress_interval == 5
    assert service.logger is service_logger

    with pytest.raises(ValueError, match="progress_interval must be greater than zero"):
        importer.ImportService("feed.xml", provider_id=42, progress_interval=0)


def test_start_run_uses_provider_name_and_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _JobRepositoryStub(new_run_id=88)
    lookup_repository = _LookupRepositoryStub(
        provider=SimpleNamespace(id=42, name="jobg8")
    )
    monkeypatch.setattr(importer, "SessionLocal", lambda: nullcontext(object()))
    monkeypatch.setattr(
        importer,
        "PromotionRepository",
        lambda session: lookup_repository,
    )
    monkeypatch.setattr(importer, "JobRepository", lambda session: repository)

    import_run_id = importer.ImportService.start_run(
        42,
        source_uri="C:/feeds/jobs.xml",
    )

    assert import_run_id == 88
    assert lookup_repository.provider_lookups == [42]
    assert repository.created_runs == [
        {"source_name": "jobg8", "source_uri": "C:/feeds/jobs.xml"}
    ]
    assert repository.flush_count == 1
    assert repository.commit_count == 1


def test_start_run_rejects_missing_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    lookup_repository = _LookupRepositoryStub(provider=None)
    monkeypatch.setattr(importer, "SessionLocal", lambda: nullcontext(object()))
    monkeypatch.setattr(
        importer,
        "PromotionRepository",
        lambda session: lookup_repository,
    )

    with pytest.raises(ValueError, match="Provider 404 does not exist"):
        importer.ImportService.start_run(404)


def test_start_run_wraps_database_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    database_error = OperationalError("SELECT 1", {}, RuntimeError("database down"))

    def fail_to_open_session() -> None:
        raise database_error

    monkeypatch.setattr(importer, "SessionLocal", fail_to_open_session)

    with pytest.raises(TransientImportError) as exc_info:
        importer.ImportService.start_run(42)

    assert "Could not create an import run for provider 42" in str(exc_info.value)
    assert exc_info.value.__cause__ is database_error


def test_mark_failed_persists_existing_run(monkeypatch: pytest.MonkeyPatch) -> None:
    run = _import_run()
    repository = _JobRepositoryStub()
    lookup_repository = _LookupRepositoryStub(run=run)
    monkeypatch.setattr(importer, "SessionLocal", lambda: nullcontext(object()))
    monkeypatch.setattr(
        importer,
        "PromotionRepository",
        lambda session: lookup_repository,
    )
    monkeypatch.setattr(importer, "JobRepository", lambda session: repository)

    result = importer.ImportService.mark_failed(run.id, RuntimeError("bad feed"))

    assert result is True
    assert repository.finished_imports == [
        {
            "run": run,
            "status": "failed",
            "received": 9,
            "staged": 8,
            "imported": 7,
            "rejected": 1,
            "error_message": "bad feed",
        }
    ]
    assert repository.commit_count == 1


def test_mark_failed_returns_false_for_missing_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_logger = Mock()
    lookup_repository = _LookupRepositoryStub(run=None)
    monkeypatch.setattr(importer, "logger", module_logger)
    monkeypatch.setattr(importer, "SessionLocal", lambda: nullcontext(object()))
    monkeypatch.setattr(
        importer,
        "PromotionRepository",
        lambda session: lookup_repository,
    )

    assert importer.ImportService.mark_failed(404, RuntimeError("failure")) is False
    module_logger.error.assert_called_once_with(
        "Cannot mark missing import run as failed",
        extra={"import_run_id": 404},
    )


def test_mark_failed_swallows_persistence_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_logger = Mock()

    def fail_to_open_session() -> None:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(importer, "logger", module_logger)
    monkeypatch.setattr(importer, "SessionLocal", fail_to_open_session)

    assert importer.ImportService.mark_failed(17, RuntimeError("failure")) is False
    module_logger.exception.assert_called_once_with(
        "Could not persist failed import outcome",
        extra={"import_run_id": 17},
    )


def test_valid_raw_record_is_normalized_inferred_and_staged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_record = _valid_raw_record(
        EmploymentType="FT",
        SalaryCurrency="US Dollar . USD",
        NewProviderField="provider value",
    )
    run = _import_run()
    repository = _JobRepositoryStub()
    service_logger = Mock()
    _configure_run_dependencies(
        monkeypatch,
        run=run,
        repository=repository,
        raw_records=[raw_record],
    )
    service = importer.ImportService(
        "feed.xml",
        provider_id=run.provider_id,
        import_run_id=run.id,
        progress_interval=100,
        service_logger=service_logger,
    )

    summary = service.run()

    assert summary == importer.ImportSummary(
        import_run_id=run.id,
        total_jobs=1,
        valid_jobs=1,
        invalid_jobs=0,
    )
    assert len(repository.staged_jobs) == 1
    staged = repository.staged_jobs[0]
    record = staged["record"]
    assert staged["run"].provider_id == 42
    assert record.sender_reference == "job-123"
    assert record.employment_type == "full_time"
    assert record.salary_currency == "USD"
    assert staged["raw_payload"] == raw_record
    assert staged["payload_hash"] == compute_payload_hash(record)
    assert staged["remote_status"] == "remote"
    assert staged["remote_status_source"] == "inferred"
    assert staged["experience_level"] == "senior"
    assert staged["experience_level_source"] == "inferred"
    assert repository.unmapped_counts == [{"NewProviderField": 1}]
    assert repository.fallback_counts == [{"salary_currency": 1}]
    assert repository.finished_imports == [
        {
            "run": run,
            "status": "completed",
            "received": 1,
            "staged": 1,
            "imported": 0,
            "rejected": 0,
        }
    ]
    assert repository.commit_count == 1
    service_logger.info.assert_not_called()


def test_empty_optional_fields_and_malformed_date_text_are_preserved_safely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_record = _valid_raw_record(
        Location="   ",
        SalaryMinimum="   ",
        LogoURL="",
        EmploymentType="",
        StartDate="32/99/not-a-date",
    )
    run = _import_run()
    repository = _JobRepositoryStub()
    _configure_run_dependencies(
        monkeypatch,
        run=run,
        repository=repository,
        raw_records=[raw_record],
    )

    summary = importer.ImportService(
        "feed.xml",
        provider_id=run.provider_id,
        import_run_id=run.id,
    ).run()

    assert summary.valid_jobs == 1
    record = repository.staged_jobs[0]["record"]
    assert record.location is None
    assert record.salary_min is None
    assert record.advertiser_logo_url is None
    assert record.employment_type is None
    assert record.start_date_text == "32/99/not-a-date"
    assert repository.unmapped_counts == [{}]
    assert repository.fallback_counts == [{}]


def test_missing_required_records_are_counted_and_not_staged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing_url = _valid_raw_record(SenderReference="missing-url")
    missing_url.pop("ApplicationURL")
    blank_title = _valid_raw_record(SenderReference="blank-title", Position="   ")
    missing_sender = _valid_raw_record()
    missing_sender.pop("SenderReference")
    run = _import_run()
    repository = _JobRepositoryStub()
    service_logger = Mock()
    _configure_run_dependencies(
        monkeypatch,
        run=run,
        repository=repository,
        raw_records=[missing_url, blank_title, missing_sender],
    )

    summary = importer.ImportService(
        "feed.xml",
        provider_id=run.provider_id,
        import_run_id=run.id,
        progress_interval=2,
        service_logger=service_logger,
    ).run()

    assert summary.total_jobs == 3
    assert summary.valid_jobs == 0
    assert summary.invalid_jobs == 3
    assert repository.staged_jobs == []
    assert repository.finished_imports[0]["received"] == 3
    assert repository.finished_imports[0]["staged"] == 0
    assert repository.finished_imports[0]["rejected"] == 3
    warning_references = [call.args[1] for call in service_logger.warning.call_args_list]
    assert warning_references == ["missing-url", "blank-title", "<missing>"]
    service_logger.info.assert_called_once_with(
        "Job feed progress: total=%d valid=%d invalid=%d",
        2,
        0,
        2,
    )


def test_unmapped_and_fallback_fields_are_aggregated_across_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_records = [
        _valid_raw_record(
            SenderReference="job-1",
            EmploymentType="bespoke",
            SalaryMinimum="invalid",
            UnknownA="first",
        ),
        _valid_raw_record(
            SenderReference="job-2",
            EmploymentType="bespoke",
            UnknownA="second",
            UnknownB="only once",
        ),
    ]
    run = _import_run()
    repository = _JobRepositoryStub()
    service_logger = Mock()
    _configure_run_dependencies(
        monkeypatch,
        run=run,
        repository=repository,
        raw_records=raw_records,
    )

    importer.ImportService(
        "feed.xml",
        provider_id=run.provider_id,
        import_run_id=run.id,
        service_logger=service_logger,
    ).run()

    assert repository.unmapped_counts == [{"UnknownA": 2, "UnknownB": 1}]
    assert repository.fallback_counts == [
        {"employment_type": 2, "salary_min": 1}
    ]
    assert [
        staged["record"].employment_type for staged in repository.staged_jobs
    ] == ["other", "other"]
    assert repository.staged_jobs[0]["raw_payload"]["EmploymentType"] == "bespoke"
    service_logger.warning.assert_any_call(
        "Feed contained unmapped fields: %s",
        "UnknownA=2, UnknownB=1",
    )
    service_logger.warning.assert_any_call(
        "Feed required field fallbacks: %s",
        "employment_type=2, salary_min=1",
    )


def test_run_creates_import_run_when_id_is_not_supplied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _import_run(run_id=88)
    repository = _JobRepositoryStub()
    start_run = Mock(return_value=run.id)
    monkeypatch.setattr(importer.ImportService, "start_run", start_run)
    _configure_run_dependencies(
        monkeypatch,
        run=run,
        repository=repository,
        raw_records=[],
    )
    service = importer.ImportService("feed.xml", provider_id=run.provider_id)

    summary = service.run()

    start_run.assert_called_once_with(
        run.provider_id,
        source_uri=str(Path("feed.xml")),
    )
    assert service.import_run_id == run.id
    assert summary.import_run_id == run.id
    assert summary.total_jobs == 0


def test_run_rejects_missing_import_run(monkeypatch: pytest.MonkeyPatch) -> None:
    repository = _JobRepositoryStub()
    mark_failed = Mock(return_value=True)
    monkeypatch.setattr(importer.ImportService, "mark_failed", mark_failed)
    _configure_run_dependencies(
        monkeypatch,
        run=None,
        repository=repository,
        raw_records=[],
    )
    service = importer.ImportService(
        "feed.xml",
        provider_id=42,
        import_run_id=404,
    )

    with pytest.raises(ValueError, match="ImportRun 404 does not exist"):
        service.run()

    assert mark_failed.call_count == 1
    assert mark_failed.call_args.args[0] == 404


def test_run_rejects_import_run_owned_by_another_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _import_run(provider_id=7)
    repository = _JobRepositoryStub()
    mark_failed = Mock(return_value=True)
    monkeypatch.setattr(importer.ImportService, "mark_failed", mark_failed)
    _configure_run_dependencies(
        monkeypatch,
        run=run,
        repository=repository,
        raw_records=[],
    )
    service = importer.ImportService(
        "feed.xml",
        provider_id=42,
        import_run_id=run.id,
    )

    with pytest.raises(
        ValueError,
        match="ImportRun 17 belongs to provider 7, not provider 42",
    ):
        service.run()

    assert mark_failed.call_count == 1
    assert mark_failed.call_args.args[0] == run.id


def test_run_wraps_operational_error_and_marks_run_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _import_run()
    repository = _JobRepositoryStub()
    database_error = OperationalError("INSERT", {}, RuntimeError("connection lost"))
    mark_failed = Mock(return_value=True)

    def failing_parser(*args: Any, **kwargs: Any) -> Iterator[JobFeedRecord]:
        raise database_error
        yield  # pragma: no cover

    monkeypatch.setattr(importer.ImportService, "mark_failed", mark_failed)
    _configure_run_dependencies(
        monkeypatch,
        run=run,
        repository=repository,
        raw_records=[],
    )
    monkeypatch.setattr(importer, "parse_job_feed", failing_parser)
    service = importer.ImportService(
        "feed.xml",
        provider_id=run.provider_id,
        import_run_id=run.id,
    )

    with pytest.raises(TransientImportError) as exc_info:
        service.run()

    assert exc_info.value.import_run_id == run.id
    assert "Temporary database failure during import" in str(exc_info.value)
    mark_failed.assert_called_once_with(run.id, database_error)


def test_run_marks_failed_and_reraises_unhandled_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _import_run()
    repository = _JobRepositoryStub()
    parse_error = RuntimeError("malformed XML")
    mark_failed = Mock(return_value=True)

    def failing_parser(*args: Any, **kwargs: Any) -> Iterator[JobFeedRecord]:
        raise parse_error
        yield  # pragma: no cover

    monkeypatch.setattr(importer.ImportService, "mark_failed", mark_failed)
    _configure_run_dependencies(
        monkeypatch,
        run=run,
        repository=repository,
        raw_records=[],
    )
    monkeypatch.setattr(importer, "parse_job_feed", failing_parser)
    service = importer.ImportService(
        "feed.xml",
        provider_id=run.provider_id,
        import_run_id=run.id,
    )

    with pytest.raises(RuntimeError) as exc_info:
        service.run()

    assert exc_info.value is parse_error
    mark_failed.assert_called_once_with(run.id, parse_error)
