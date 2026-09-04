"""Unit tests for provider import scheduling decisions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.db.repositories import ProviderScheduleState
from app.imports.scheduler import ProviderDispatchPlan, ProviderSchedulerService

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


class _SchedulerRepositoryStub:
    """Model the repository contract: only active providers are ever returned.

    The real ``list_active_provider_schedules`` filters ``providers.is_active``
    in SQL (asserted in ``tests/test_repositories.py``), so inactive providers
    are dropped here rather than being handed to the service.
    """

    def __init__(
        self,
        schedules: list[ProviderScheduleState],
        *,
        inactive_provider_ids: frozenset[int] = frozenset(),
    ) -> None:
        self.schedules = [
            schedule
            for schedule in schedules
            if schedule.provider_id not in inactive_provider_ids
        ]
        self.list_call_count = 0

    def list_active_provider_schedules(self) -> list[ProviderScheduleState]:
        self.list_call_count += 1
        return self.schedules


def _state(
    provider_id: int,
    *,
    interval_minutes: int | None = 60,
    last_completed_at: datetime | None = NOW,
    has_processing_import: bool = False,
) -> ProviderScheduleState:
    return ProviderScheduleState(
        provider_id=provider_id,
        provider_name=f"provider-{provider_id}",
        schedule_interval_minutes=interval_minutes,
        last_completed_at=last_completed_at,
        has_processing_import=has_processing_import,
    )


def _plan(
    schedules: list[ProviderScheduleState],
    *,
    now: datetime | None = NOW,
    inactive_provider_ids: frozenset[int] = frozenset(),
) -> ProviderDispatchPlan:
    repository = _SchedulerRepositoryStub(
        schedules, inactive_provider_ids=inactive_provider_ids
    )
    plan = ProviderSchedulerService(repository).build_dispatch_plan(now=now)
    assert repository.list_call_count == 1
    return plan


def test_classifies_every_active_provider_by_its_dispatch_reason() -> None:
    plan = _plan(
        [
            _state(1, last_completed_at=NOW - timedelta(minutes=61)),
            _state(2, last_completed_at=NOW - timedelta(minutes=59)),
            _state(3, interval_minutes=None),
            _state(4, has_processing_import=True),
        ]
    )

    assert plan.checked_provider_ids == [1, 2, 3, 4]
    assert plan.due_provider_ids == [1]
    assert plan.skipped_not_due_provider_ids == [2]
    assert plan.skipped_unconfigured_provider_ids == [3]
    assert plan.skipped_processing_provider_ids == [4]


def test_provider_that_never_completed_an_import_is_due_immediately() -> None:
    plan = _plan([_state(7, last_completed_at=None)])

    assert plan.due_provider_ids == [7]
    assert plan.skipped_not_due_provider_ids == []


def test_provider_is_due_exactly_at_its_next_import_time() -> None:
    plan = _plan([_state(7, last_completed_at=NOW - timedelta(minutes=60))])

    assert plan.due_provider_ids == [7]


def test_a_running_import_wins_over_every_other_reason() -> None:
    plan = _plan(
        [
            _state(
                7,
                interval_minutes=None,
                last_completed_at=None,
                has_processing_import=True,
            )
        ]
    )

    assert plan.skipped_processing_provider_ids == [7]
    assert plan.due_provider_ids == []
    assert plan.skipped_unconfigured_provider_ids == []


def test_unconfigured_provider_is_never_due_however_stale_it_is() -> None:
    plan = _plan(
        [_state(7, interval_minutes=None, last_completed_at=NOW - timedelta(days=30))]
    )

    assert plan.skipped_unconfigured_provider_ids == [7]
    assert plan.due_provider_ids == []


def test_inactive_providers_are_never_checked_or_dispatched() -> None:
    plan = _plan(
        [
            _state(1, last_completed_at=None),
            _state(2, last_completed_at=None),
        ],
        inactive_provider_ids=frozenset({2}),
    )

    assert plan.checked_provider_ids == [1]
    assert plan.due_provider_ids == [1]
    assert 2 not in plan.skipped_not_due_provider_ids
    assert 2 not in plan.skipped_unconfigured_provider_ids
    assert 2 not in plan.skipped_processing_provider_ids


def test_no_active_providers_produces_an_empty_plan() -> None:
    plan = _plan([])

    assert plan.checked_provider_ids == []
    assert plan.due_provider_ids == []
    assert plan.skipped_not_due_provider_ids == []
    assert plan.skipped_processing_provider_ids == []
    assert plan.skipped_unconfigured_provider_ids == []


def test_every_checked_provider_lands_in_exactly_one_bucket() -> None:
    plan = _plan(
        [
            _state(1, last_completed_at=NOW - timedelta(minutes=61)),
            _state(2, last_completed_at=NOW - timedelta(minutes=59)),
            _state(3, interval_minutes=None),
            _state(4, has_processing_import=True),
        ]
    )
    classified = (
        plan.due_provider_ids
        + plan.skipped_not_due_provider_ids
        + plan.skipped_unconfigured_provider_ids
        + plan.skipped_processing_provider_ids
    )

    assert sorted(classified) == plan.checked_provider_ids
    assert len(set(classified)) == len(classified)


def test_falls_back_to_the_current_time_when_no_clock_is_supplied() -> None:
    now = datetime.now(tz=UTC)
    plan = _plan(
        [
            _state(1, last_completed_at=now - timedelta(days=1)),
            _state(2, last_completed_at=now + timedelta(days=1)),
        ],
        now=None,
    )

    assert plan.due_provider_ids == [1]
    assert plan.skipped_not_due_provider_ids == [2]
