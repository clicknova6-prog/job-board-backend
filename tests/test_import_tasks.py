"""Unit tests for the provider import Celery task wiring.

These tests assert orchestration only: which service each branch calls, with
which arguments, and how the task reports the outcome. ``ImportService``,
``PromotionService`` and ``DownloadService`` have their own suites, so their
behaviour is never re-tested here.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import MagicMock, Mock

import pytest
from celery.exceptions import Retry
from sqlalchemy.exc import OperationalError

# app.celery_app is imported first on purpose: its autodiscovery loads every
# task module, so importing a task module first is a circular import.
from app.celery_app import celery_app
from app.imports.exceptions import TransientImportError
from app.imports.importer import ImportSummary
from app.imports.promotion import PromotionSummary
from app.services import import_orchestration_service
from app.tasks.import_tasks import run_provider_import

PROVIDER_ID = 7
RUN_ID = 101
FEED_URL = "https://feeds.example/jobg8.zip"
XML_PATH = Path("feed.xml")
MAX_RETRIES = 3
IMPORT_SUMMARY = ImportSummary(
    import_run_id=RUN_ID,
    total_jobs=10,
    valid_jobs=9,
    invalid_jobs=1,
)
PROMOTION_SUMMARY = PromotionSummary(
    new_jobs=3,
    updated_jobs=2,
    unchanged_jobs=4,
    deactivated_jobs=1,
)

_UNSET = object()


class _SessionStub:
    """Record the transaction boundaries the task is expected to use."""

    def __init__(self) -> None:
        self.rollback_count = 0
        self.close_count = 0

    def rollback(self) -> None:
        self.rollback_count += 1

    def close(self) -> None:
        self.close_count += 1


class _PromotionRepositoryStub:
    """Stand in for both the repository class and the instance it builds."""

    def __init__(
        self,
        *,
        provider: SimpleNamespace | None,
        run: SimpleNamespace | None,
    ) -> None:
        self.provider = provider
        self.run = run
        self.sessions: list[_SessionStub] = []
        self.provider_lookups: list[int] = []
        self.run_lookups: list[int] = []

    def __call__(self, session: _SessionStub) -> _PromotionRepositoryStub:
        self.sessions.append(session)
        return self

    def get_provider(self, provider_id: int) -> SimpleNamespace | None:
        self.provider_lookups.append(provider_id)
        return self.provider

    def get_import_run(self, import_run_id: int) -> SimpleNamespace | None:
        self.run_lookups.append(import_run_id)
        return self.run


class _DownloadedFeedStub:
    """Minimal context manager mirroring ``DownloadedFeed``."""

    def __init__(self, xml_path: Path = XML_PATH) -> None:
        self.xml_path = xml_path
        self.exit_count = 0

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.exit_count += 1


def _provider(*, is_active: bool = True) -> SimpleNamespace:
    return SimpleNamespace(id=PROVIDER_ID, feed_url=FEED_URL, is_active=is_active)


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    *,
    provider: Any = _UNSET,
    run: Any = _UNSET,
) -> SimpleNamespace:
    """Replace every collaborator of the task with an inspectable double."""
    session = _SessionStub()
    repository = _PromotionRepositoryStub(
        provider=_provider() if provider is _UNSET else provider,
        run=SimpleNamespace(id=RUN_ID, status="completed") if run is _UNSET else run,
    )
    feed = _DownloadedFeedStub()

    import_service = Mock(name="ImportService")
    import_service.start_run.return_value = RUN_ID
    import_service.return_value.run.return_value = IMPORT_SUMMARY

    promotion_service = Mock(name="PromotionService")
    promotion_service.return_value.run.return_value = PROMOTION_SUMMARY

    download_service = MagicMock(name="DownloadService")
    download_service.return_value.download.return_value = feed

    module = import_orchestration_service
    monkeypatch.setattr(module, "SessionLocal", lambda: session)
    monkeypatch.setattr(module, "PromotionRepository", repository)
    monkeypatch.setattr(module, "ImportService", import_service)
    monkeypatch.setattr(module, "PromotionService", promotion_service)
    monkeypatch.setattr(module, "DownloadService", download_service)

    return SimpleNamespace(
        session=session,
        repository=repository,
        feed=feed,
        import_service=import_service,
        promotion_service=promotion_service,
        download_service=download_service,
    )


def _forbid_sleeping(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly if anything tries to wait out a real backoff delay."""

    def _never_sleep(seconds: float) -> None:
        raise AssertionError(f"test slept for {seconds} seconds")

    monkeypatch.setattr(time, "sleep", _never_sleep)


def _run_task(*, retries: int = MAX_RETRIES) -> Any:
    """Execute the task eagerly with a controlled retry counter.

    Celery's eager ``apply`` re-runs the task itself for each retry, so tests
    that are not about retrying start on the final attempt to keep one run.
    """
    return run_provider_import.apply(args=[PROVIDER_ID], retries=retries)


def test_successful_run_calls_each_service_with_the_expected_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wiring = _wire(monkeypatch)

    result = _run_task().result

    assert wiring.repository.sessions == [wiring.session]
    assert wiring.repository.provider_lookups == [PROVIDER_ID]
    wiring.import_service.start_run.assert_called_once_with(
        PROVIDER_ID, source_uri=FEED_URL
    )
    wiring.download_service.return_value.download.assert_called_once_with(
        wiring.repository.provider
    )
    wiring.import_service.assert_called_once_with(
        XML_PATH, provider_id=PROVIDER_ID, import_run_id=RUN_ID
    )
    wiring.import_service.return_value.run.assert_called_once_with()
    wiring.promotion_service.assert_called_once_with(RUN_ID)
    wiring.promotion_service.return_value.run.assert_called_once_with()
    wiring.import_service.mark_failed.assert_not_called()
    assert result == {
        "status": "completed",
        "provider_id": PROVIDER_ID,
        "import_run_id": RUN_ID,
        "records_received": 10,
        "records_staged": 9,
        "records_rejected": 1,
        "new_jobs": 3,
        "updated_jobs": 2,
        "unchanged_jobs": 4,
        "deactivated_jobs": 1,
    }


def test_successful_run_releases_the_feed_and_the_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wiring = _wire(monkeypatch)

    _run_task()

    assert wiring.feed.exit_count == 1
    assert wiring.session.close_count == 1


def test_import_run_status_is_re_read_through_the_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An anomaly-aborted promotion returns normally, so the authoritative
    # status has to come from a rolled-back re-read of import_runs.
    wiring = _wire(monkeypatch, run=SimpleNamespace(id=RUN_ID, status="failed"))

    result = _run_task().result

    assert wiring.session.rollback_count == 1
    assert wiring.repository.run_lookups == [RUN_ID]
    assert result["status"] == "failed"
    assert result["new_jobs"] == 3


def test_missing_import_run_after_promotion_is_reported_as_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire(monkeypatch, run=None)

    assert _run_task().result["status"] == "failed"


def test_unknown_provider_short_circuits_before_any_service_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wiring = _wire(monkeypatch, provider=None)

    result = _run_task().result

    wiring.import_service.start_run.assert_not_called()
    wiring.download_service.assert_not_called()
    wiring.promotion_service.assert_not_called()
    wiring.import_service.mark_failed.assert_not_called()
    assert result == {
        "status": "failed",
        "provider_id": PROVIDER_ID,
        "import_run_id": None,
        "error": f"Provider {PROVIDER_ID} does not exist",
    }
    assert wiring.session.close_count == 1


def test_inactive_provider_is_never_imported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wiring = _wire(monkeypatch, provider=_provider(is_active=False))

    result = _run_task().result

    wiring.import_service.start_run.assert_not_called()
    wiring.download_service.assert_not_called()
    assert result["status"] == "failed"
    assert result["error"] == f"Provider {PROVIDER_ID} is inactive"


def test_transient_failure_retries_up_to_the_configured_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _forbid_sleeping(monkeypatch)
    wiring = _wire(monkeypatch)
    error = TransientImportError("feed host unreachable")
    wiring.download_service.return_value.download.side_effect = error

    outcome = run_provider_import.apply(args=[PROVIDER_ID], retries=0)

    # Eager apply runs the retry ladder inline: the first attempt plus one per
    # allowed retry, after which the task reports failure instead of retrying.
    assert wiring.repository.provider_lookups == [PROVIDER_ID] * (MAX_RETRIES + 1)
    assert wiring.import_service.mark_failed.call_count == MAX_RETRIES + 1
    wiring.import_service.mark_failed.assert_called_with(RUN_ID, error)
    assert outcome.state == "SUCCESS"
    assert outcome.result == {
        "status": "failed",
        "provider_id": PROVIDER_ID,
        "import_run_id": RUN_ID,
        "error": str(error),
    }


@pytest.mark.parametrize("retries", [0, 1, 2])
def test_retry_is_scheduled_with_bounded_backoff_and_no_waiting(
    monkeypatch: pytest.MonkeyPatch, retries: int
) -> None:
    _forbid_sleeping(monkeypatch)
    wiring = _wire(monkeypatch)
    error = TransientImportError("feed host unreachable")
    wiring.download_service.return_value.download.side_effect = error

    # Drive one attempt in isolation so the scheduled countdown is observable
    # instead of being consumed by the eager retry ladder.
    run_provider_import.push_request(
        retries=retries, is_eager=True, called_directly=False
    )
    try:
        with pytest.raises(Retry) as exc_info:
            run_provider_import.run(PROVIDER_ID)
    finally:
        run_provider_import.pop_request()

    assert exc_info.value.exc is error
    assert 0 <= exc_info.value.when <= run_provider_import.retry_backoff_max


def test_database_outage_is_converted_into_a_transient_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wiring = _wire(monkeypatch)
    wiring.repository.get_provider = Mock(
        side_effect=OperationalError(
            "SELECT providers", {}, RuntimeError("database unavailable")
        )
    )

    result = _run_task().result

    # The outage struck before a run existed, so there is nothing to mark.
    wiring.import_service.mark_failed.assert_not_called()
    assert result["status"] == "failed"
    assert result["import_run_id"] is None
    assert "Temporary database failure" in result["error"]


def test_database_outage_after_the_run_started_is_marked_and_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _forbid_sleeping(monkeypatch)
    wiring = _wire(monkeypatch)
    wiring.promotion_service.return_value.run.side_effect = OperationalError(
        "UPDATE jobs", {}, RuntimeError("database unavailable")
    )

    result = run_provider_import.apply(args=[PROVIDER_ID], retries=0).result

    marked_run_id, marked_error = wiring.import_service.mark_failed.call_args.args
    assert marked_run_id == RUN_ID
    assert isinstance(marked_error, TransientImportError)
    # A converted database outage is retried like any other transient failure.
    assert wiring.import_service.mark_failed.call_count == MAX_RETRIES + 1
    assert result["status"] == "failed"
    assert result["import_run_id"] == RUN_ID


def test_unexpected_error_is_recorded_and_never_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _forbid_sleeping(monkeypatch)
    wiring = _wire(monkeypatch)
    error = ValueError("malformed feed archive")
    wiring.import_service.return_value.run.side_effect = error

    outcome = run_provider_import.apply(args=[PROVIDER_ID], retries=0)

    assert outcome.state == "SUCCESS"
    assert wiring.repository.provider_lookups == [PROVIDER_ID]
    assert outcome.result == {
        "status": "failed",
        "provider_id": PROVIDER_ID,
        "import_run_id": RUN_ID,
        "error": str(error),
    }
    wiring.import_service.mark_failed.assert_called_once_with(RUN_ID, error)
    assert wiring.feed.exit_count == 1
    assert wiring.session.close_count == 1


def test_retry_policy_matches_the_documented_configuration() -> None:
    assert run_provider_import.name == "app.tasks.import_tasks.run_provider_import"
    assert run_provider_import.name in celery_app.tasks
    assert run_provider_import.max_retries == MAX_RETRIES
    assert run_provider_import.autoretry_for == (TransientImportError,)
    assert run_provider_import.retry_backoff is True
    assert run_provider_import.retry_backoff_max == 600
    assert run_provider_import.retry_jitter is True


def test_log_context_reports_the_task_identity_and_duration() -> None:
    context = import_orchestration_service._log_context(PROVIDER_ID, RUN_ID, 0.0)

    assert context["provider_id"] == PROVIDER_ID
    assert context["import_run_id"] == RUN_ID
    assert context["duration_seconds"] >= 0
