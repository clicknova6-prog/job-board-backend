"""Unit tests for the Celery Beat dispatcher that enqueues provider imports."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Self
from unittest.mock import Mock

import pytest

# app.celery_app is imported first on purpose: its autodiscovery loads every
# task module, so importing a task module first is a circular import.
from app.celery_app import celery_app
from app.imports.scheduler import ProviderDispatchPlan
from app.tasks import scheduler_tasks
from app.tasks.scheduler_tasks import dispatch_provider_imports


class _SessionContextStub:
    """Mirror the ``with SessionLocal() as session`` contract."""

    def __init__(self) -> None:
        self.entered = 0
        self.exited = 0

    def __enter__(self) -> Self:
        self.entered += 1
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.exited += 1


def _plan(
    *,
    checked: list[int],
    due: list[int],
    not_due: list[int] | None = None,
    processing: list[int] | None = None,
    unconfigured: list[int] | None = None,
) -> ProviderDispatchPlan:
    return ProviderDispatchPlan(
        checked_provider_ids=checked,
        due_provider_ids=due,
        skipped_not_due_provider_ids=not_due or [],
        skipped_processing_provider_ids=processing or [],
        skipped_unconfigured_provider_ids=unconfigured or [],
    )


def _wire(
    monkeypatch: pytest.MonkeyPatch, plan: ProviderDispatchPlan
) -> SimpleNamespace:
    """Replace the session, repository, scheduler, and enqueued task."""
    session = _SessionContextStub()
    repository = Mock(name="SchedulerRepository")
    scheduler_service = Mock(name="ProviderSchedulerService")
    scheduler_service.return_value.build_dispatch_plan.return_value = plan
    import_task = Mock(name="run_provider_import")

    monkeypatch.setattr(scheduler_tasks, "SessionLocal", lambda: session)
    monkeypatch.setattr(scheduler_tasks, "SchedulerRepository", repository)
    monkeypatch.setattr(scheduler_tasks, "ProviderSchedulerService", scheduler_service)
    monkeypatch.setattr(scheduler_tasks, "run_provider_import", import_task)

    return SimpleNamespace(
        session=session,
        repository=repository,
        scheduler_service=scheduler_service,
        import_task=import_task,
    )


def test_due_providers_are_enqueued_through_the_scheduler_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wiring = _wire(
        monkeypatch,
        _plan(checked=[1, 2, 3], due=[1, 3], not_due=[2]),
    )

    result = dispatch_provider_imports.apply().result

    wiring.repository.assert_called_once_with(wiring.session)
    wiring.scheduler_service.assert_called_once_with(wiring.repository.return_value)
    wiring.scheduler_service.return_value.build_dispatch_plan.assert_called_once_with()
    assert wiring.import_task.delay.call_args_list == [((1,),), ((3,),)]
    assert wiring.session.entered == 1
    assert wiring.session.exited == 1
    assert result == {
        "checked_provider_ids": [1, 2, 3],
        "enqueued_provider_ids": [1, 3],
        "skipped_not_due_provider_ids": [2],
        "skipped_processing_provider_ids": [],
        "skipped_unconfigured_provider_ids": [],
    }


def test_result_renames_due_providers_to_the_ones_actually_enqueued(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire(monkeypatch, _plan(checked=[1], due=[1]))

    result = dispatch_provider_imports.apply().result

    assert "due_provider_ids" not in result
    assert result["enqueued_provider_ids"] == [1]


def test_nothing_is_enqueued_when_no_provider_is_due(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wiring = _wire(
        monkeypatch,
        _plan(checked=[1, 2], due=[], processing=[1], unconfigured=[2]),
    )

    result = dispatch_provider_imports.apply().result

    wiring.import_task.delay.assert_not_called()
    assert result["enqueued_provider_ids"] == []
    assert result["skipped_processing_provider_ids"] == [1]
    assert result["skipped_unconfigured_provider_ids"] == [2]


def test_no_active_providers_dispatches_an_empty_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wiring = _wire(monkeypatch, _plan(checked=[], due=[]))

    result = dispatch_provider_imports.apply().result

    wiring.import_task.delay.assert_not_called()
    assert result["checked_provider_ids"] == []
    assert result["enqueued_provider_ids"] == []


def test_dispatch_task_is_registered_under_its_documented_name() -> None:
    assert (
        dispatch_provider_imports.name
        == "app.tasks.scheduler_tasks.dispatch_provider_imports"
    )
    assert dispatch_provider_imports.name in celery_app.tasks
