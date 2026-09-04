"""End-to-end staged -> promoted pipeline test against a real PostgreSQL database.

``tests/test_promotion.py`` covers PromotionService's decision logic with a
repository stub; this test covers the other half — that those decisions
actually land in PostgreSQL. It seeds one provider, a previous import run, and
a representative mix of staged records through the ORM, runs the real
``PromotionService.run()`` against the real ``PromotionRepository``, and then
asserts on the rows the database is left holding. It is the automated
equivalent of the manual ``scripts/test_import_cycle.py`` workflow.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.db.models import AffiliateLink, ImportRun, Job, JobStaging, Provider
from app.imports import promotion
from app.imports.promotion import PromotionService, PromotionSummary, slugify

# A dedicated provider per test keeps these rows out of each other's way and
# out of the way of the API suite, which shares the same test database but
# scopes itself to its own provider.
PROVIDER_NAME = "integration-pipeline-provider"
ANOMALY_PROVIDER_NAME = "integration-anomaly-provider"
OWNED_PROVIDER_NAMES = (PROVIDER_NAME, ANOMALY_PROVIDER_NAME)

PREVIOUS_RUN_AT = datetime.now(UTC) - timedelta(hours=1)

NEW_TITLE = "Senior Platform Engineer"
ADVERTISER_NAME = "Integration Co"
UPDATED_TITLE = "Staff Backend Engineer"
UNCHANGED_TITLE = "Data Engineer"
STALE_TITLE = "Withdrawn Engineer"
PRESERVED_SHORT_HASH = "integration-unchanged-hash"


@dataclass(frozen=True, slots=True)
class SeededPipeline:
    """Identifiers of the rows seeded before promotion runs."""

    provider_id: int
    previous_run_id: int
    run_id: int
    updated_job_id: int
    updated_job_slug: str
    unchanged_job_id: int
    stale_job_id: int


def _sync_driver_url(url: str) -> str:
    """Select the psycopg 3 driver the same way ``app.db.session`` does."""
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def _purge_provider_rows(session: Session, provider_name: str) -> None:
    """Delete every row one of this module's providers owns, FK-safe order.

    ``jobs.last_seen_import_run_id`` is ON DELETE RESTRICT, so jobs go before
    import runs. Staging rows cascade from the run, affiliate links from the job.
    """
    provider_id = session.scalar(
        select(Provider.id).where(Provider.name == provider_name)
    )
    if provider_id is None:
        return

    session.execute(delete(Job).where(Job.provider_id == provider_id))
    session.execute(delete(ImportRun).where(ImportRun.provider_id == provider_id))
    session.execute(delete(Provider).where(Provider.id == provider_id))
    session.commit()


@pytest.fixture()
def session_factory(test_database_url: str) -> Iterator[sessionmaker[Session]]:
    """Yield a sessionmaker bound to the migrated test database."""
    engine = create_engine(_sync_driver_url(test_database_url), poolclass=NullPool)
    factory = sessionmaker(
        bind=engine, class_=Session, autoflush=False, expire_on_commit=False
    )
    try:
        yield factory
    finally:
        engine.dispose()


@pytest.fixture()
def promotion_against_test_database(
    monkeypatch: pytest.MonkeyPatch, session_factory: sessionmaker[Session]
) -> None:
    """Point PromotionService's own session at the test database.

    The service opens its sessions itself, so this is the seam that keeps a
    real run off the developer database. Everything below it — repository,
    SQL, transaction boundaries — stays exactly as production runs it.
    """
    monkeypatch.setattr(promotion, "SessionLocal", session_factory)


@pytest.fixture()
def clean_database(session_factory: sessionmaker[Session]) -> Iterator[None]:
    """Remove this module's rows before and after, so reruns start from empty."""
    with session_factory() as session:
        for provider_name in OWNED_PROVIDER_NAMES:
            _purge_provider_rows(session, provider_name)
    yield
    with session_factory() as session:
        for provider_name in OWNED_PROVIDER_NAMES:
            _purge_provider_rows(session, provider_name)


def _raw_payload(source_job_id: str, title: str) -> dict[str, object]:
    """Build a raw feed payload shaped like the Jobg8 elements we stage."""
    return {
        "SenderReference": source_job_id,
        "Title": title,
        "URL": f"https://example.test/apply/{source_job_id}",
    }


def _stage(
    session: Session,
    *,
    run_id: int,
    source_job_id: str,
    title: str,
    payload_hash: str,
) -> JobStaging:
    """Insert one valid staged record for the run."""
    staged = JobStaging(
        import_run_id=run_id,
        source_job_id=source_job_id,
        raw_payload=_raw_payload(source_job_id, title),
        payload_hash=payload_hash,
        advertiser_name=ADVERTISER_NAME,
        classification="Information Technology",
        title=title,
        description=f"Staged description for {source_job_id}.",
        country_name="Australia",
        location="Sydney",
        apply_url=f"https://example.test/apply/{source_job_id}",
        employment_type="Full Time",
        is_valid=True,
    )
    session.add(staged)
    return staged


def _existing_job(
    *,
    provider_name: str,
    provider_id: int,
    previous_run_id: int,
    source_job_id: str,
    title: str,
    payload_hash: str,
) -> Job:
    """Build an active job left behind by the previous import run."""
    return Job(
        source_name=provider_name,
        provider_id=provider_id,
        source_job_id=source_job_id,
        slug=f"existing-{source_job_id}",
        advertiser_name=ADVERTISER_NAME,
        classification="Information Technology",
        title=title,
        description=f"Previous description for {source_job_id}.",
        country_name="Australia",
        location="Sydney",
        apply_url=f"https://example.test/apply/{source_job_id}",
        employment_type="Full Time",
        source_payload=_raw_payload(source_job_id, title),
        payload_hash=payload_hash,
        last_seen_import_run_id=previous_run_id,
        is_active=True,
        first_imported_at=PREVIOUS_RUN_AT,
        last_imported_at=PREVIOUS_RUN_AT,
    )


def _seed(session_factory: sessionmaker[Session]) -> SeededPipeline:
    """Seed a provider, a previous run and its jobs, and the run about to promote.

    The staged batch is a representative mix: one record never seen before
    (new), one whose payload hash moved (update), one whose hash is identical
    (unchanged), plus an active job the batch does not contain at all (stale).
    Three staged against three active keeps the feed-drop anomaly check at 0%,
    so this run promotes rather than aborting.
    """
    with session_factory() as session:
        provider = Provider(name=PROVIDER_NAME, format="xml", config={})
        session.add(provider)
        session.flush()

        previous_run = ImportRun(
            source_name=PROVIDER_NAME,
            provider_id=provider.id,
            status="completed",
            records_received=3,
            records_staged=3,
            records_imported=3,
        )
        run = ImportRun(
            source_name=PROVIDER_NAME,
            provider_id=provider.id,
            status="processing",
            records_received=3,
            records_staged=3,
            records_rejected=0,
        )
        session.add_all([previous_run, run])
        session.flush()

        updated_job = _existing_job(
            provider_name=PROVIDER_NAME,
            provider_id=provider.id,
            previous_run_id=previous_run.id,
            source_job_id="job-updated",
            title="Backend Engineer",
            payload_hash="hash-updated-v1",
        )
        unchanged_job = _existing_job(
            provider_name=PROVIDER_NAME,
            provider_id=provider.id,
            previous_run_id=previous_run.id,
            source_job_id="job-unchanged",
            title=UNCHANGED_TITLE,
            payload_hash="hash-unchanged",
        )
        stale_job = _existing_job(
            provider_name=PROVIDER_NAME,
            provider_id=provider.id,
            previous_run_id=previous_run.id,
            source_job_id="job-stale",
            title=STALE_TITLE,
            payload_hash="hash-stale",
        )
        session.add_all([updated_job, unchanged_job, stale_job])
        session.flush()

        # Only the unchanged job starts with a link, so the run has to mint one
        # for the new job, backfill the update, and leave this short_hash — a
        # public, possibly already-shared identifier — untouched.
        session.add(
            AffiliateLink(
                short_hash=PRESERVED_SHORT_HASH,
                job_id=unchanged_job.id,
                provider_id=provider.id,
            )
        )

        _stage(
            session,
            run_id=run.id,
            source_job_id="job-new",
            title=NEW_TITLE,
            payload_hash="hash-new",
        )
        _stage(
            session,
            run_id=run.id,
            source_job_id="job-updated",
            title=UPDATED_TITLE,
            payload_hash="hash-updated-v2",
        )
        _stage(
            session,
            run_id=run.id,
            source_job_id="job-unchanged",
            title=UNCHANGED_TITLE,
            payload_hash="hash-unchanged",
        )

        session.commit()

        return SeededPipeline(
            provider_id=provider.id,
            previous_run_id=previous_run.id,
            run_id=run.id,
            updated_job_id=updated_job.id,
            updated_job_slug=updated_job.slug,
            unchanged_job_id=unchanged_job.id,
            stale_job_id=stale_job.id,
        )


@pytest.mark.usefixtures("clean_database", "promotion_against_test_database")
def test_promotion_run_lands_the_full_staged_batch_in_postgres(
    session_factory: sessionmaker[Session],
) -> None:
    """Promote a mixed staged batch and assert on the resulting database rows."""
    seeded = _seed(session_factory)

    summary = PromotionService(seeded.run_id).run()

    assert summary == PromotionSummary(
        new_jobs=1, updated_jobs=1, unchanged_jobs=1, deactivated_jobs=1
    )

    with session_factory() as session:
        jobs = {
            job.source_job_id: job
            for job in session.scalars(
                select(Job).where(Job.provider_id == seeded.provider_id)
            )
        }
        assert set(jobs) == {"job-new", "job-updated", "job-unchanged", "job-stale"}

        active_count = session.scalar(
            select(func.count())
            .select_from(Job)
            .where(Job.provider_id == seeded.provider_id, Job.is_active.is_(True))
        )
        assert active_count == 3

        new_job = jobs["job-new"]
        assert new_job.is_active is True
        assert new_job.deactivated_at is None
        assert new_job.title == NEW_TITLE
        assert new_job.payload_hash == "hash-new"
        assert new_job.last_seen_import_run_id == seeded.run_id
        assert new_job.first_imported_at == new_job.last_imported_at
        expected_slug_base = slugify(f"{NEW_TITLE}-{ADVERTISER_NAME}")
        assert new_job.slug == f"{expected_slug_base}-{new_job.id}"
        # source_payload holds the raw feed record verbatim, never normalized values.
        assert new_job.source_payload == _raw_payload("job-new", NEW_TITLE)

        updated_job = jobs["job-updated"]
        assert updated_job.id == seeded.updated_job_id
        assert updated_job.is_active is True
        assert updated_job.title == UPDATED_TITLE
        assert updated_job.description == "Staged description for job-updated."
        assert updated_job.payload_hash == "hash-updated-v2"
        assert updated_job.last_seen_import_run_id == seeded.run_id
        assert updated_job.last_imported_at > PREVIOUS_RUN_AT
        # The slug is a public URL: a changed title must not move it.
        assert updated_job.slug == seeded.updated_job_slug

        unchanged_job = jobs["job-unchanged"]
        assert unchanged_job.id == seeded.unchanged_job_id
        assert unchanged_job.is_active is True
        assert unchanged_job.payload_hash == "hash-unchanged"
        assert unchanged_job.last_seen_import_run_id == seeded.run_id
        # Unchanged still means seen: the timestamp moves, the content does not.
        assert unchanged_job.last_imported_at > PREVIOUS_RUN_AT
        assert unchanged_job.description == "Previous description for job-unchanged."

        stale_job = jobs["job-stale"]
        assert stale_job.id == seeded.stale_job_id
        assert stale_job.is_active is False
        assert stale_job.deactivated_at is not None
        # Soft delete only: the row and its content survive the retention window.
        assert stale_job.last_seen_import_run_id == seeded.previous_run_id
        assert stale_job.title == STALE_TITLE

        links = {
            link.job_id: link
            for link in session.scalars(
                select(AffiliateLink).where(
                    AffiliateLink.provider_id == seeded.provider_id
                )
            )
        }
        # Eligible means active: the deactivated job never gets a link.
        assert set(links) == {new_job.id, updated_job.id, unchanged_job.id}
        assert links[unchanged_job.id].short_hash == PRESERVED_SHORT_HASH
        assert links[new_job.id].short_hash
        assert links[updated_job.id].short_hash
        assert len({link.short_hash for link in links.values()}) == 3

        run = session.get(ImportRun, seeded.run_id)
        assert run is not None
        assert run.status == "completed"
        assert run.error_message is None
        assert run.is_anomalous is False
        assert run.anomaly_reasons == []
        assert run.new_jobs == 1
        assert run.updated_jobs == 1
        assert run.deleted_jobs == 1
        # records_imported counts writes to `jobs`, so the unchanged row is excluded.
        assert run.records_imported == 2

        # The previous run is a historical record; promotion must not rewrite it.
        previous_run = session.get(ImportRun, seeded.previous_run_id)
        assert previous_run is not None
        assert previous_run.status == "completed"

        # Staging is an audit trail, not a queue: promotion reads it, never drains it.
        staged_count = session.scalar(
            select(func.count())
            .select_from(JobStaging)
            .where(JobStaging.import_run_id == seeded.run_id)
        )
        assert staged_count == 3


# _run_anomaly_check reads anomaly_drop_threshold_pct from Provider.config and
# falls back to 20. This provider configures 25 to prove the check honours the
# per-provider value; 2 staged against 10 active is an 80% drop, past both.
ANOMALY_DROP_THRESHOLD_PCT = 25
ANOMALY_REJECTION_RATE_PCT = 15
ANOMALY_ACTIVE_JOB_COUNT = 10
ANOMALY_STAGED_JOB_COUNT = 2

# Every jobs column promotion is capable of writing. `updated_at` is in here
# because the task asks for it, but it carries no onupdate and no trigger, so
# on its own it would prove little -- the rest of the tuple is what makes an
# unchanged snapshot mean the rows were genuinely never written.
PROMOTION_MUTABLE_COLUMNS = (
    "title",
    "description",
    "slug",
    "payload_hash",
    "source_payload",
    "is_active",
    "deactivated_at",
    "first_imported_at",
    "last_imported_at",
    "content_updated_at",
    "last_seen_import_run_id",
    "updated_at",
)


@dataclass(frozen=True, slots=True)
class SeededAnomaly:
    """Identifiers of the rows seeded before the anomalous run promotes."""

    provider_id: int
    previous_run_id: int
    run_id: int


def _job_snapshot(session: Session, provider_id: int) -> dict[str, dict[str, object]]:
    """Capture every promotion-writable column of a provider's jobs."""
    return {
        job.source_job_id: {
            column: getattr(job, column) for column in PROMOTION_MUTABLE_COLUMNS
        }
        for job in session.scalars(select(Job).where(Job.provider_id == provider_id))
    }


def _seed_anomalous(session_factory: sessionmaker[Session]) -> SeededAnomaly:
    """Seed a healthy catalogue and a run whose feed collapsed to a fraction of it.

    The two staged records are updates to jobs that already exist, with moved
    payload hashes and changed titles, so a run that promoted would visibly
    rewrite them and deactivate the other eight. None of that may happen.
    """
    with session_factory() as session:
        provider = Provider(
            name=ANOMALY_PROVIDER_NAME,
            format="xml",
            config={
                "anomaly_drop_threshold_pct": ANOMALY_DROP_THRESHOLD_PCT,
                "anomaly_rejection_rate_pct": ANOMALY_REJECTION_RATE_PCT,
            },
        )
        session.add(provider)
        session.flush()

        previous_run = ImportRun(
            source_name=ANOMALY_PROVIDER_NAME,
            provider_id=provider.id,
            status="completed",
            records_received=ANOMALY_ACTIVE_JOB_COUNT,
            records_staged=ANOMALY_ACTIVE_JOB_COUNT,
            records_imported=ANOMALY_ACTIVE_JOB_COUNT,
        )
        run = ImportRun(
            source_name=ANOMALY_PROVIDER_NAME,
            provider_id=provider.id,
            status="processing",
            records_received=ANOMALY_STAGED_JOB_COUNT,
            records_staged=ANOMALY_STAGED_JOB_COUNT,
            # No rejections, so the drop check is the only one that can trip.
            records_rejected=0,
        )
        session.add_all([previous_run, run])
        session.flush()

        session.add_all(
            [
                _existing_job(
                    provider_name=ANOMALY_PROVIDER_NAME,
                    provider_id=provider.id,
                    previous_run_id=previous_run.id,
                    source_job_id=f"anomaly-job-{index}",
                    title=f"Established Engineer {index}",
                    payload_hash=f"hash-anomaly-{index}",
                )
                for index in range(ANOMALY_ACTIVE_JOB_COUNT)
            ]
        )

        for index in range(ANOMALY_STAGED_JOB_COUNT):
            _stage(
                session,
                run_id=run.id,
                source_job_id=f"anomaly-job-{index}",
                title=f"Rewritten Engineer {index}",
                payload_hash=f"hash-anomaly-{index}-v2",
            )

        session.commit()

        return SeededAnomaly(
            provider_id=provider.id,
            previous_run_id=previous_run.id,
            run_id=run.id,
        )


@pytest.mark.usefixtures("clean_database", "promotion_against_test_database")
def test_promotion_aborts_on_anomalous_feed_drop_leaving_jobs_untouched(
    session_factory: sessionmaker[Session],
) -> None:
    """An anomalous run must fail loudly and write nothing to the catalogue."""
    seeded = _seed_anomalous(session_factory)

    with session_factory() as session:
        before = _job_snapshot(session, seeded.provider_id)
    assert len(before) == ANOMALY_ACTIVE_JOB_COUNT

    summary = PromotionService(seeded.run_id).run()

    assert summary == PromotionSummary(
        new_jobs=0, updated_jobs=0, unchanged_jobs=0, deactivated_jobs=0
    )

    with session_factory() as session:
        after = _job_snapshot(session, seeded.provider_id)

        # Column for column, row for row: not one job was written.
        assert after == before
        assert len(after) == ANOMALY_ACTIVE_JOB_COUNT
        assert all(row["is_active"] is True for row in after.values())
        assert all(row["deactivated_at"] is None for row in after.values())
        assert all(
            row["updated_at"] == before[source_job_id]["updated_at"]
            for source_job_id, row in after.items()
        )
        # The staged updates did not land, and the run never claimed these rows.
        assert all(
            row["last_seen_import_run_id"] == seeded.previous_run_id
            for row in after.values()
        )
        assert all(
            str(row["title"]).startswith("Established Engineer")
            for row in after.values()
        )

        # Affiliate generation and backfill both sit past the abort return.
        link_count = session.scalar(
            select(func.count())
            .select_from(AffiliateLink)
            .where(AffiliateLink.provider_id == seeded.provider_id)
        )
        assert link_count == 0

        run = session.get(ImportRun, seeded.run_id)
        assert run is not None
        assert run.status == "failed"
        assert run.is_anomalous is True
        assert run.anomaly_reasons == ["catalogue_drop"]
        assert run.records_imported == 0
        assert run.new_jobs == 0
        assert run.updated_jobs == 0
        assert run.deleted_jobs == 0
        assert run.error_message is not None
        assert run.error_message.startswith("Promotion aborted due to anomaly:")
        assert (
            f"anomaly_drop_threshold_pct={ANOMALY_DROP_THRESHOLD_PCT}"
            in run.error_message
        )

        # The staged rows survive the abort, so the run can be investigated.
        staged_count = session.scalar(
            select(func.count())
            .select_from(JobStaging)
            .where(JobStaging.import_run_id == seeded.run_id)
        )
        assert staged_count == ANOMALY_STAGED_JOB_COUNT
