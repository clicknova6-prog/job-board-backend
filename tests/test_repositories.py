"""Unit tests for the synchronous import-pipeline repositories."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest
from sqlalchemy.orm import Session

from app.db.models import ImportRun, Job, JobStaging, Provider
from app.db.repositories import (
    CleanupRepository,
    JobRepository,
    PromotionRepository,
    ProviderRetentionPolicy,
    ProviderScheduleState,
    SchedulerRepository,
    SitemapJob,
    SitemapRepository,
    _mask_sensitive_query_parameters,
    _staged_field_values,
)
from app.imports.schemas import JobFeedRecord

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


def _session() -> Mock:
    return Mock(spec=Session)


def _run(*, run_id: int = 17, provider_id: int = 7) -> ImportRun:
    return ImportRun(
        id=run_id,
        provider_id=provider_id,
        source_name="jobg8",
        status="processing",
        records_received=0,
        records_staged=0,
        records_imported=0,
        records_rejected=0,
        new_jobs=0,
        updated_jobs=0,
        deleted_jobs=0,
        is_anomalous=False,
        anomaly_reasons=[],
    )


def _record(**overrides: object) -> JobFeedRecord:
    raw: dict[str, object] = {
        "AdvertiserName": "Acme",
        "AdvertiserType": "Employer",
        "SenderReference": "source-123",
        "DisplayReference": "display-123",
        "Classification": "Technology",
        "Position": "Senior Remote Engineer",
        "Description": "Build distributed systems.",
        "Country": "United States",
        "Location": "Remote",
        "Area": "North America",
        "PostalCode": "00123",
        "ApplicationURL": "https://example.com/jobs/123",
        "Language": "1033",
        "EmploymentType": "FT",
        "StartDate": "Immediate",
        "Duration": "Permanent",
        "WorkHours": "Full Time",
        "SalaryCurrency": "usd",
        "SalaryMinimum": "100000.50",
        "SalaryMaximum": "150000",
        "SalaryPeriod": "Annual",
        "SalaryAdditional": "Bonus",
        "LogoURL": "https://example.com/logo.png",
        "JobType": "ATS",
        "SellPrice": "1.2500",
        "SellPriceCurrency": "USD",
        "RevenueType": "CPC",
    }
    raw.update(overrides)
    return JobFeedRecord.model_validate(raw)


def _staged(
    *,
    source_job_id: str = "source-123",
    payload_hash: str = "hash-1",
    title: str = "Senior Remote Engineer",
) -> JobStaging:
    return JobStaging(
        id=31,
        import_run_id=17,
        source_job_id=source_job_id,
        raw_payload={"SenderReference": source_job_id},
        payload_hash=payload_hash,
        advertiser_name="Acme",
        advertiser_type="Employer",
        display_reference="display-123",
        classification="Technology",
        title=title,
        description="Build distributed systems.",
        country_name="United States",
        location="Remote",
        area="North America",
        postal_code="00123",
        apply_url="https://example.com/jobs/123",
        language_code="1033",
        employment_type="full_time",
        start_date_text="Immediate",
        duration="Permanent",
        work_hours="Full Time",
        salary_currency="USD",
        salary_min=Decimal("100000.50"),
        salary_max=Decimal(150000),
        salary_period="Annual",
        salary_additional="Bonus",
        advertiser_logo_url="https://example.com/logo.png",
        job_type="ATS",
        remote_status="remote",
        remote_status_source="inferred",
        experience_level="senior",
        experience_level_source="inferred",
        validation_errors=[],
        is_valid=True,
    )


def _statement_params(statement: object) -> dict[str, object]:
    return statement.compile().params  # type: ignore[attr-defined, no-any-return]


@pytest.mark.parametrize(
    ("source_uri", "expected"),
    [
        (None, None),
        ("C:/feeds/jobs.xml", "C:/feeds/jobs.xml"),
        (
            "https://feeds.example/jobs?api_key=secret&region=us&custom-token=x&flag#part",
            "https://feeds.example/jobs?api_key=****&region=us&custom-token=****&flag#part",
        ),
        (
            "https://feeds.example/jobs?User%20Name=alice&accountNumber=42",
            "https://feeds.example/jobs?User%20Name=****&accountNumber=****",
        ),
    ],
)
def test_mask_sensitive_query_parameters(
    source_uri: str | None,
    expected: str | None,
) -> None:
    assert _mask_sensitive_query_parameters(source_uri) == expected


def test_create_import_run_resolves_provider_masks_uri_and_adds_row() -> None:
    session = _session()
    session.scalar.return_value = 7
    repository = JobRepository(session)

    run = repository.create_import_run(
        "jobg8",
        source_uri="https://feeds.example/jobs?token=secret&region=us",
        source_checksum="checksum",
    )

    assert run.provider_id == 7
    assert run.source_name == "jobg8"
    assert run.source_uri == "https://feeds.example/jobs?token=****&region=us"
    assert run.source_checksum == "checksum"
    assert run.status == "processing"
    session.add.assert_called_once_with(run)
    statement = session.scalar.call_args.args[0]
    assert "providers.name" in str(statement)
    assert "jobg8" in _statement_params(statement).values()


def test_create_import_run_rejects_unknown_provider() -> None:
    session = _session()
    session.scalar.return_value = None
    repository = JobRepository(session)

    with pytest.raises(ValueError, match="No Provider row named 'missing'"):
        repository.create_import_run("missing")

    session.add.assert_not_called()


def test_job_repository_records_diagnostics_and_finishes_import() -> None:
    session = _session()
    repository = JobRepository(session)
    run = _run()
    unmapped = {"FutureField": 2}
    fallbacks = {"salary_min": 1}

    repository.record_unmapped_fields(run, unmapped)
    repository.record_field_fallback_warnings(run, fallbacks)
    unmapped["LaterMutation"] = 9
    fallbacks["LaterMutation"] = 9
    repository.finish_import(
        run,
        status="failed",
        received=10,
        staged=8,
        imported=3,
        rejected=2,
        error_message="validation threshold exceeded",
    )

    assert run.unmapped_fields == {"FutureField": 2}
    assert run.field_fallback_warnings == {"salary_min": 1}
    assert run.status == "failed"
    assert run.completed_at is not None
    assert run.completed_at.tzinfo is UTC
    assert run.records_received == 10
    assert run.records_staged == 8
    assert run.records_imported == 3
    assert run.records_rejected == 2
    assert run.error_message == "validation threshold exceeded"


def test_stage_job_maps_validated_record_to_staging_row() -> None:
    session = _session()
    repository = JobRepository(session)
    run = _run()
    record = _record()
    raw_payload = dict(record.source_record)

    staged = repository.stage_job(
        run,
        record,
        payload_hash="payload-hash",
        raw_payload=raw_payload,
        remote_status="remote",
        remote_status_source="inferred",
        experience_level="senior",
        experience_level_source="inferred",
    )

    assert staged.import_run_id == run.id
    assert staged.source_job_id == record.sender_reference
    assert staged.raw_payload is raw_payload
    assert staged.payload_hash == "payload-hash"
    assert staged.title == record.title
    assert staged.description == record.description
    assert staged.apply_url == "https://example.com/jobs/123"
    assert staged.employment_type == "full_time"
    assert staged.salary_currency == "USD"
    assert staged.salary_min == Decimal("100000.50")
    assert staged.advertiser_logo_url == "https://example.com/logo.png"
    assert staged.remote_status == "remote"
    assert staged.experience_level == "senior"
    assert staged.validation_errors == []
    assert staged.is_valid is True
    session.add.assert_called_once_with(staged)


def test_stage_job_handles_missing_optional_logo() -> None:
    session = _session()
    repository = JobRepository(session)

    staged = repository.stage_job(
        _run(),
        _record(LogoURL=""),
        payload_hash="hash",
        raw_payload={},
        remote_status=None,
        remote_status_source=None,
        experience_level=None,
        experience_level_source=None,
    )

    assert staged.advertiser_logo_url is None
    assert staged.remote_status is None


def test_job_repository_session_helpers_delegate() -> None:
    session = _session()
    repository = JobRepository(session)

    repository.flush()
    repository.commit()
    repository.rollback()

    session.flush.assert_called_once_with()
    session.commit.assert_called_once_with()
    session.rollback.assert_called_once_with()


def test_identity_constraints_scope_jobs_and_staging_rows() -> None:
    job_constraints = {constraint.name for constraint in Job.__table__.constraints}
    staging_constraints = {
        constraint.name for constraint in JobStaging.__table__.constraints
    }

    assert "jobs_provider_source_job_unique" in job_constraints
    assert "job_staging_import_run_source_job_unique" in staging_constraints


def test_staged_field_values_contains_every_promoted_feed_field() -> None:
    staged = _staged()

    values = _staged_field_values(staged)

    assert values == {
        "advertiser_name": "Acme",
        "advertiser_type": "Employer",
        "display_reference": "display-123",
        "classification": "Technology",
        "title": "Senior Remote Engineer",
        "description": "Build distributed systems.",
        "country_name": "United States",
        "location": "Remote",
        "area": "North America",
        "postal_code": "00123",
        "apply_url": "https://example.com/jobs/123",
        "language_code": "1033",
        "employment_type": "full_time",
        "start_date_text": "Immediate",
        "duration": "Permanent",
        "work_hours": "Full Time",
        "salary_currency": "USD",
        "salary_min": Decimal("100000.50"),
        "salary_max": Decimal(150000),
        "salary_period": "Annual",
        "salary_additional": "Bonus",
        "advertiser_logo_url": "https://example.com/logo.png",
        "job_type": "ATS",
        "remote_status": "remote",
        "remote_status_source": "inferred",
        "experience_level": "senior",
        "experience_level_source": "inferred",
        "source_payload": {"SenderReference": "source-123"},
        "payload_hash": "hash-1",
    }


def test_promotion_repository_primary_key_reads_include_not_found() -> None:
    session = _session()
    run = _run()
    provider = Provider(id=7, name="jobg8", format="xml", config={})
    session.get.side_effect = [run, provider, None]
    repository = PromotionRepository(session)

    assert repository.get_import_run(17) is run
    assert repository.get_provider(7) is provider
    assert repository.get_import_run(404) is None
    assert session.get.call_args_list == [
        call(ImportRun, 17),
        call(Provider, 7),
        call(ImportRun, 404),
    ]


@pytest.mark.parametrize(("database_count", "expected"), [(5, 5), (None, 0)])
def test_count_active_jobs_handles_count_and_empty_result(
    database_count: int | None,
    expected: int,
) -> None:
    session = _session()
    session.scalar.return_value = database_count
    repository = PromotionRepository(session)

    assert repository.count_active_jobs(7) == expected
    statement = session.scalar.call_args.args[0]
    assert "jobs.provider_id" in str(statement)
    assert 7 in _statement_params(statement).values()


def test_load_valid_staged_jobs_returns_ordered_rows_and_empty_results() -> None:
    session = _session()
    staged_rows = [_staged(source_job_id="one"), _staged(source_job_id="two")]
    session.scalars.side_effect = [staged_rows, []]
    repository = PromotionRepository(session)

    assert repository.load_valid_staged_jobs(17) == staged_rows
    assert repository.load_valid_staged_jobs(18) == []
    first_statement = session.scalars.call_args_list[0].args[0]
    assert "job_staging.import_run_id" in str(first_statement)
    assert "ORDER BY job_staging.id" in str(first_statement)
    assert 17 in _statement_params(first_statement).values()


def test_job_lookup_is_scoped_by_provider_and_source_reference() -> None:
    session = _session()
    job = Job(id=1, provider_id=7, source_name="jobg8", source_job_id="shared")
    session.scalar.side_effect = [job, None]
    repository = PromotionRepository(session)

    assert repository.get_job_by_provider_and_source(7, "shared") is job
    assert repository.get_job_by_provider_and_source(8, "shared") is None
    first_statement = session.scalar.call_args_list[0].args[0]
    sql = str(first_statement)
    parameters = _statement_params(first_statement).values()
    assert "jobs.provider_id" in sql
    assert "jobs.source_job_id" in sql
    assert 7 in parameters
    assert "shared" in parameters


def test_create_update_and_mark_seen_mutate_one_canonical_job() -> None:
    session = _session()
    repository = PromotionRepository(session)
    run = _run()
    original_staged = _staged()

    job = repository.create_job(
        run=run,
        staged=original_staged,
        placeholder_slug="__pending__7__source-123",
        now=NOW,
    )

    assert job.provider_id == 7
    assert job.source_job_id == "source-123"
    assert job.slug == "__pending__7__source-123"
    assert job.title == "Senior Remote Engineer"
    assert job.source_payload == original_staged.raw_payload
    assert job.payload_hash == "hash-1"
    assert job.first_imported_at == NOW
    assert job.last_imported_at == NOW
    assert job.content_updated_at == NOW
    assert job.last_seen_import_run_id == run.id
    assert job.is_active is True
    session.add.assert_called_once_with(job)

    job.id = 100
    job.slug = "stable-slug-100"
    first_imported_at = job.first_imported_at
    changed_at = NOW + timedelta(hours=1)
    changed_staged = _staged(payload_hash="hash-2", title="Principal Engineer")
    repository.update_job_from_staged(job, changed_staged, run_id=18, now=changed_at)
    repository.update_job_from_staged(job, changed_staged, run_id=18, now=changed_at)

    assert job.id == 100
    assert job.slug == "stable-slug-100"
    assert job.first_imported_at == first_imported_at
    assert job.title == "Principal Engineer"
    assert job.payload_hash == "hash-2"
    assert job.last_imported_at == changed_at
    assert job.content_updated_at == changed_at
    assert job.last_seen_import_run_id == 18
    # Reapplying an identical staged record updates the same ORM object and
    # never asks the session to add a second canonical row.
    session.add.assert_called_once_with(job)

    seen_at = NOW + timedelta(hours=2)
    repository.mark_job_seen(job, run_id=19, now=seen_at)
    assert job.last_imported_at == seen_at
    assert job.last_seen_import_run_id == 19
    assert job.content_updated_at == changed_at


def test_deactivate_stale_jobs_executes_scoped_bulk_update() -> None:
    session = _session()
    session.execute.return_value = SimpleNamespace(rowcount=3)
    repository = PromotionRepository(session)

    count = repository.deactivate_stale_jobs(7, 17, NOW)

    assert count == 3
    statement = session.execute.call_args.args[0]
    sql = str(statement)
    parameters = _statement_params(statement).values()
    assert sql.startswith("UPDATE jobs")
    assert "jobs.provider_id" in sql
    assert "jobs.last_seen_import_run_id" in sql
    assert 7 in parameters
    assert 17 in parameters
    assert NOW in parameters


def test_finish_promotion_copies_outcome_and_anomaly_reasons() -> None:
    repository = PromotionRepository(_session())
    run = _run()
    reasons = ["catalogue_drop"]

    repository.finish_promotion(
        run,
        status="failed",
        records_imported=4,
        new_jobs=1,
        updated_jobs=3,
        deleted_jobs=2,
        error_message="anomaly",
        is_anomalous=True,
        anomaly_reasons=reasons,
    )
    reasons.append("later-mutation")

    assert run.status == "failed"
    assert run.records_imported == 4
    assert run.new_jobs == 1
    assert run.updated_jobs == 3
    assert run.deleted_jobs == 2
    assert run.error_message == "anomaly"
    assert run.is_anomalous is True
    assert run.anomaly_reasons == ["catalogue_drop"]

    repository.finish_promotion(run, status="completed", records_imported=0)
    assert run.anomaly_reasons == []


def test_promotion_repository_session_helpers_delegate() -> None:
    session = _session()
    repository = PromotionRepository(session)

    repository.flush()
    repository.commit()
    repository.rollback()

    session.flush.assert_called_once_with()
    session.commit.assert_called_once_with()
    session.rollback.assert_called_once_with()


def test_scheduler_repository_maps_rows_and_empty_result() -> None:
    session = _session()
    rows = [
        SimpleNamespace(
            id=7,
            name="jobg8",
            schedule_interval_minutes=60,
            last_completed_at=NOW,
            has_processing_import=True,
        ),
        SimpleNamespace(
            id=8,
            name="backup",
            schedule_interval_minutes=None,
            last_completed_at=None,
            has_processing_import=False,
        ),
    ]
    session.execute.side_effect = [rows, []]
    repository = SchedulerRepository(session)

    assert repository.list_active_provider_schedules() == [
        ProviderScheduleState(7, "jobg8", 60, NOW, True),
        ProviderScheduleState(8, "backup", None, None, False),
    ]
    assert repository.list_active_provider_schedules() == []
    statement = session.execute.call_args_list[0].args[0]
    assert "providers.is_active" in str(statement)
    assert "ORDER BY providers.id" in str(statement)


def test_cleanup_repository_lists_policies_and_expired_id_batch() -> None:
    session = _session()
    policy_rows = [
        SimpleNamespace(id=7, name="jobg8", deleted_job_retention_hours=12),
        SimpleNamespace(id=8, name="backup", deleted_job_retention_hours=None),
    ]
    session.execute.side_effect = [policy_rows, []]
    session.scalars.side_effect = [[3, 5], []]
    repository = CleanupRepository(session)
    cutoff = NOW - timedelta(hours=12)

    assert repository.list_provider_retention_policies() == [
        ProviderRetentionPolicy(7, "jobg8", 12),
        ProviderRetentionPolicy(8, "backup", None),
    ]
    assert repository.list_provider_retention_policies() == []
    assert repository.find_expired_job_ids(
        provider_id=7,
        cutoff=cutoff,
        limit=2,
    ) == [3, 5]
    assert repository.find_expired_job_ids(
        provider_id=7,
        cutoff=cutoff,
        limit=2,
    ) == []
    statement = session.scalars.call_args_list[0].args[0]
    sql = str(statement)
    parameters = _statement_params(statement).values()
    assert "jobs.provider_id" in sql
    assert "jobs.deactivated_at" in sql
    assert "ORDER BY jobs.id" in sql
    assert 7 in parameters
    assert cutoff in parameters
    assert 2 in parameters


def test_cleanup_hard_delete_rechecks_scope_and_handles_empty_batch() -> None:
    session = _session()
    session.execute.return_value = SimpleNamespace(rowcount=2)
    repository = CleanupRepository(session)
    cutoff = NOW - timedelta(hours=12)

    assert repository.hard_delete_expired_jobs(
        job_ids=[],
        provider_id=7,
        cutoff=cutoff,
    ) == 0
    session.execute.assert_not_called()

    assert repository.hard_delete_expired_jobs(
        job_ids=[3, 5],
        provider_id=7,
        cutoff=cutoff,
    ) == 2
    statement = session.execute.call_args.args[0]
    sql = str(statement)
    parameters = _statement_params(statement).values()
    assert sql.startswith("DELETE FROM jobs")
    assert "jobs.provider_id" in sql
    assert [3, 5] in parameters
    assert 7 in parameters
    assert cutoff in parameters


def test_cleanup_repository_session_helpers_delegate() -> None:
    session = _session()
    repository = CleanupRepository(session)

    repository.commit()
    repository.rollback()

    session.commit.assert_called_once_with()
    session.rollback.assert_called_once_with()


def test_sitemap_repository_returns_keyset_page_and_empty_result() -> None:
    session = _session()
    rows = [
        SimpleNamespace(id=11, slug="job-11", last_imported_at=NOW),
        SimpleNamespace(
            id=12,
            slug="job-12",
            last_imported_at=NOW + timedelta(minutes=1),
        ),
    ]
    session.execute.side_effect = [rows, []]
    repository = SitemapRepository(session)

    assert repository.list_active_jobs_after(after_id=10, limit=2) == [
        SitemapJob(11, "job-11", NOW),
        SitemapJob(12, "job-12", NOW + timedelta(minutes=1)),
    ]
    assert repository.list_active_jobs_after(after_id=12, limit=2) == []
    statement = session.execute.call_args_list[0].args[0]
    sql = str(statement)
    parameters = _statement_params(statement).values()
    assert "jobs.is_active" in sql
    assert "jobs.id >" in sql
    assert "ORDER BY jobs.id" in sql
    assert 10 in parameters
    assert 2 in parameters
