"""Tests for promotion anomaly outcomes."""

from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any

import pytest

from app.db.models import ImportRun
from app.db.repositories import PromotionRepository as DatabasePromotionRepository
from app.imports import promotion


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
        return 0

    def finish_promotion(self, run: ImportRun, **kwargs: Any) -> None:
        DatabasePromotionRepository.finish_promotion(self, run, **kwargs)

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass


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
