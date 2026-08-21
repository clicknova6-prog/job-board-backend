"""Persistence layer for the job import pipeline.

This module contains repository classes that encapsulate all database access
for the import feature. Repositories accept a SQLAlchemy Session through
their constructor and are the only place where ORM objects are created,
queried, or modified.

Rules enforced here:
- No business logic. Repositories only read and write data.
- No raw SQL. All access goes through the SQLAlchemy ORM.
- No session lifecycle management beyond flush/commit/rollback helpers.
  The caller (service layer) decides when to commit.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.db.models import ImportRun, Job, JobStaging, Provider
from app.imports.schemas import JobFeedRecord


class JobRepository:
    """Data access methods for the job import pipeline.

    All methods operate on the session provided at construction time.
    The caller is responsible for committing or rolling back the transaction.

    Example usage::

        with SessionLocal() as session:
            repo = JobRepository(session)
            run = repo.create_import_run(source_name="jobg8", source_uri="https://...")
            repo.commit()
    """

    def __init__(self, session: Session) -> None:
        """Store the active SQLAlchemy session for use by all methods.

        Args:
            session: An open SQLAlchemy ORM session. The repository does not
                open or close this session — that is the caller's responsibility.
        """
        self._session = session

    # ------------------------------------------------------------------
    # ImportRun methods
    # ------------------------------------------------------------------

    def create_import_run(
        self,
        source_name: str,
        source_uri: str | None = None,
        source_checksum: str | None = None,
    ) -> ImportRun:
        """Insert a new ImportRun row and return the ORM object.

        The run is created with status='processing'. The started_at and
        created_at timestamps are populated by PostgreSQL server defaults on
        INSERT. The caller should call flush() or commit() after this to make
        the generated primary key available.

        provider_id is resolved from source_name here rather than being passed
        in, so that callers which still identify a feed by name keep working
        while source_name is being deprecated in favour of provider_id.

        Args:
            source_name: The configured provider name, e.g. "jobg8". This is
                operational metadata, not a value from the XML feed itself.
                A Provider row with this name must already exist.
            source_uri: Optional URL or file path the feed was fetched from.
            source_checksum: Optional checksum (e.g. MD5 or SHA-256 hex) of
                the raw feed file, used to detect unchanged feeds without
                re-parsing.

        Returns:
            The newly created ImportRun ORM object, not yet flushed.

        Raises:
            ValueError: If no Provider row matches source_name.
        """
        provider_id = self._session.scalar(
            select(Provider.id).where(Provider.name == source_name)
        )
        if provider_id is None:
            raise ValueError(
                f"No Provider row named {source_name!r}; seed it before importing"
            )

        run = ImportRun(
            source_name=source_name,
            provider_id=provider_id,
            source_uri=source_uri,
            source_checksum=source_checksum,
            status="processing",
        )
        self._session.add(run)
        return run

    def record_unmapped_fields(self, run: ImportRun, counts: dict[str, int]) -> None:
        """Store the unrecognized feed element names seen during a run.

        Args:
            run: The ImportRun ORM object returned by create_import_run().
            counts: Unknown XML element name -> number of records it appeared
                in. An empty dict is stored as-is, matching the column default.
        """
        run.unmapped_fields = dict(counts)

    def record_field_fallback_warnings(
        self, run: ImportRun, counts: dict[str, int]
    ) -> None:
        """Store which fields needed fallback handling during a run.

        Args:
            run: The ImportRun ORM object returned by create_import_run().
            counts: Field name -> number of records in which that field's raw
                value required fallback handling, whether the fallback
                recovered a value or produced None.
        """
        run.field_fallback_warnings = dict(counts)

    def finish_import(
        self,
        run: ImportRun,
        status: str,
        received: int,
        staged: int,
        imported: int,
        rejected: int,
        error_message: str | None = None,
    ) -> None:
        """Update an ImportRun with final counters and completion timestamp.

        This should be called once after the full import pipeline finishes,
        whether successfully or not. It does not commit — the caller decides
        when to commit.

        Args:
            run: The ImportRun ORM object returned by create_import_run().
            status: Final status string. Must be one of: 'completed', 'failed'.
                The model check constraint enforces valid values at the DB level.
            received: Total number of <Job> records seen in the XML feed.
            staged: Number of records successfully inserted into job_staging.
            imported: Number of records promoted into the canonical jobs table.
                Pass 0 until promotion is implemented.
            rejected: Number of records that failed validation or could not be
                staged due to errors.
            error_message: Optional description of a top-level failure, such as
                a feed download error or an unhandled exception. Per-record
                errors belong in validation_errors on JobStaging, not here.
        """
        run.status = status
        run.completed_at = datetime.now(tz=UTC)
        run.records_received = received
        run.records_staged = staged
        run.records_imported = imported
        run.records_rejected = rejected
        run.error_message = error_message

    # ------------------------------------------------------------------
    # JobStaging methods
    # ------------------------------------------------------------------

    def stage_job(
        self,
        run: ImportRun,
        record: JobFeedRecord,
        payload_hash: str,
        raw_payload: dict[str, object],
    ) -> JobStaging:
        """Insert one JobStaging row for a parsed feed record.

        Every column on JobStaging that has a corresponding field on
        JobFeedRecord is populated here. Fields that exist only in the feed
        for commercial/provider purposes (sell_price, revenue_type, etc.) are
        not mapped to dedicated columns — they live in raw_payload only.

        apply_url and advertiser_logo_url are Pydantic AnyHttpUrl objects and
        are explicitly converted to plain strings before storage, since the
        ORM column type is Text.

        All staged rows start with is_valid=True and validation_errors=[].
        Validation logic belongs in the service layer, not here.

        Args:
            run: The ImportRun this staged record belongs to.
            record: A validated JobFeedRecord parsed from a single <Job> element.
            payload_hash: A hex digest of the serialized record content, used
                later for change detection during upsert (not yet implemented).
            raw_payload: The complete raw XML field dictionary for this job,
                including provider-only fields not mapped to columns.

        Returns:
            The newly created JobStaging ORM object, not yet flushed.
        """
        staged = JobStaging(
            import_run_id=run.id,
            source_job_id=record.sender_reference,
            raw_payload=raw_payload,
            payload_hash=payload_hash,
            # --- Advertiser fields ---
            advertiser_name=record.advertiser_name,
            advertiser_type=record.advertiser_type,
            display_reference=record.display_reference,
            # --- Classification and title ---
            classification=record.classification,
            title=record.title,
            description=record.description,
            # --- Location fields ---
            country_name=record.country_name,
            location=record.location,
            area=record.area,
            postal_code=record.postal_code,
            # --- Application URL ---
            # AnyHttpUrl must be cast to str — storing the Pydantic object
            # directly would persist its repr, not the URL string.
            apply_url=str(record.apply_url),
            # --- Job metadata ---
            language_code=record.language_code,
            employment_type=record.employment_type,
            start_date_text=record.start_date_text,
            duration=record.duration,
            work_hours=record.work_hours,
            # --- Salary fields ---
            salary_currency=record.salary_currency,
            salary_min=record.salary_min,
            salary_max=record.salary_max,
            salary_period=record.salary_period,
            salary_additional=record.salary_additional,
            # --- Logo URL (also AnyHttpUrl — same cast needed) ---
            advertiser_logo_url=(
                str(record.advertiser_logo_url)
                if record.advertiser_logo_url is not None
                else None
            ),
            # --- Provider/commercial job type ---
            job_type=record.job_type,
            # --- Validation state (set by service layer later if needed) ---
            validation_errors=list(),
            is_valid=True,
        )
        self._session.add(staged)
        return staged

    # ------------------------------------------------------------------
    # Session helpers
    # ------------------------------------------------------------------

    def flush(self) -> None:
        """Flush pending ORM changes to the database without committing.

        Use this when you need generated primary keys (e.g. run.id) to be
        available before the transaction is committed — for example, before
        passing a newly created ImportRun to stage_job().
        """
        self._session.flush()

    def commit(self) -> None:
        """Commit the current transaction.

        After a commit, all ORM objects in the session reflect their persisted
        state. Call this once at the end of a successful import operation.
        """
        self._session.commit()

    def rollback(self) -> None:
        """Roll back the current transaction.

        Call this in an exception handler to undo all changes made since the
        last commit. The session remains usable after a rollback.
        """
        self._session.rollback()


def _staged_field_values(staged: JobStaging) -> dict[str, object]:
    """Return the Job columns copied verbatim from a validated staged row.

    Shared by PromotionRepository.create_job() and update_job_from_staged()
    so insert and update stay in sync as columns are added.
    """
    return {
        "advertiser_name": staged.advertiser_name,
        "advertiser_type": staged.advertiser_type,
        "display_reference": staged.display_reference,
        "classification": staged.classification,
        "title": staged.title,
        "description": staged.description,
        "country_name": staged.country_name,
        "location": staged.location,
        "area": staged.area,
        "postal_code": staged.postal_code,
        "apply_url": staged.apply_url,
        "language_code": staged.language_code,
        "employment_type": staged.employment_type,
        "start_date_text": staged.start_date_text,
        "duration": staged.duration,
        "work_hours": staged.work_hours,
        "salary_currency": staged.salary_currency,
        "salary_min": staged.salary_min,
        "salary_max": staged.salary_max,
        "salary_period": staged.salary_period,
        "salary_additional": staged.salary_additional,
        "advertiser_logo_url": staged.advertiser_logo_url,
        "job_type": staged.job_type,
        "source_payload": staged.raw_payload,
        "payload_hash": staged.payload_hash,
    }


class PromotionRepository:
    """Data access methods for promoting staged jobs into the canonical jobs table.

    All methods operate on the session provided at construction time.
    The caller (PromotionService) decides when to flush, commit, or roll back.
    """

    def __init__(self, session: Session) -> None:
        """Store the active SQLAlchemy session for use by all methods.

        Args:
            session: An open SQLAlchemy ORM session. The repository does not
                open or close this session — that is the caller's responsibility.
        """
        self._session = session

    # ------------------------------------------------------------------
    # Read methods
    # ------------------------------------------------------------------

    def get_import_run(self, import_run_id: int) -> ImportRun | None:
        """Look up an ImportRun by primary key, or None if it doesn't exist."""
        return self._session.get(ImportRun, import_run_id)

    def get_provider(self, provider_id: int) -> Provider | None:
        """Look up a Provider by primary key, or None if it doesn't exist."""
        return self._session.get(Provider, provider_id)

    def count_active_jobs(self, provider_id: int) -> int:
        """Count currently-active Job rows for a provider."""
        count = self._session.scalar(
            select(func.count())
            .select_from(Job)
            .where(Job.provider_id == provider_id, Job.is_active.is_(True))
        )
        return count or 0

    def load_valid_staged_jobs(self, import_run_id: int) -> list[JobStaging]:
        """Load all is_valid=true JobStaging rows for a run, ordered by id.

        The ordering keeps batch boundaries deterministic across repeated
        calls, which matters since promotion commits one batch at a time.
        """
        return list(
            self._session.scalars(
                select(JobStaging)
                .where(
                    JobStaging.import_run_id == import_run_id,
                    JobStaging.is_valid.is_(True),
                )
                .order_by(JobStaging.id)
            )
        )

    def get_job_by_provider_and_source(
        self, provider_id: int, source_job_id: str
    ) -> Job | None:
        """Look up the canonical Job for a provider's source_job_id, if any."""
        return self._session.scalar(
            select(Job).where(
                Job.provider_id == provider_id,
                Job.source_job_id == source_job_id,
            )
        )

    # ------------------------------------------------------------------
    # Write methods
    # ------------------------------------------------------------------

    def create_job(
        self,
        *,
        run: ImportRun,
        staged: JobStaging,
        placeholder_slug: str,
        now: datetime,
    ) -> Job:
        """Insert a new Job from a staged row and return the ORM object.

        slug is set to placeholder_slug here because the column is NOT NULL
        and unique, but the real slug needs this row's generated id. The
        caller must flush(), then overwrite job.slug with the final value
        before committing.
        """
        job = Job(
            source_name=run.source_name,
            provider_id=run.provider_id,
            source_job_id=staged.source_job_id,
            slug=placeholder_slug,
            last_seen_import_run_id=run.id,
            is_active=True,
            first_imported_at=now,
            last_imported_at=now,
            **_staged_field_values(staged),
        )
        self._session.add(job)
        return job

    def update_job_from_staged(
        self, job: Job, staged: JobStaging, run_id: int, now: datetime
    ) -> None:
        """Overwrite a Job's feed-sourced fields from a changed staged row.

        first_imported_at, slug, and id are intentionally left untouched —
        the slug must stay stable once assigned, even if the title changes.
        """
        for column, value in _staged_field_values(staged).items():
            setattr(job, column, value)
        job.last_imported_at = now
        job.last_seen_import_run_id = run_id

    def mark_job_seen(self, job: Job, run_id: int, now: datetime) -> None:
        """Record that an unchanged Job was still present in this run's feed."""
        job.last_imported_at = now
        job.last_seen_import_run_id = run_id

    def deactivate_stale_jobs(
        self, provider_id: int, run_id: int, now: datetime
    ) -> int:
        """Soft-delete active jobs for a provider that this run did not see.

        Uses an ORM-enabled bulk UPDATE rather than loading each Job, since a
        provider's active set can be large. Returns the number of rows affected.
        """
        result = self._session.execute(
            update(Job)
            .where(
                Job.provider_id == provider_id,
                Job.is_active.is_(True),
                Job.last_seen_import_run_id.is_distinct_from(run_id),
            )
            .values(is_active=False, deactivated_at=now)
        )
        return result.rowcount

    def finish_promotion(
        self,
        run: ImportRun,
        status: str,
        records_imported: int,
        new_jobs: int = 0,
        updated_jobs: int = 0,
        deleted_jobs: int = 0,
        error_message: str | None = None,
    ) -> None:
        """Update an ImportRun with the outcome of the promotion phase.

        status must be one of the values allowed by the model's check
        constraint. records_imported and error_message follow the same
        semantics as JobRepository.finish_import(). new_jobs, updated_jobs,
        and deleted_jobs default to 0 for the anomaly-abort path, where the
        jobs table was never touched.
        """
        run.status = status
        run.records_imported = records_imported
        run.new_jobs = new_jobs
        run.updated_jobs = updated_jobs
        run.deleted_jobs = deleted_jobs
        run.error_message = error_message

    # ------------------------------------------------------------------
    # Session helpers
    # ------------------------------------------------------------------

    def flush(self) -> None:
        """Flush pending ORM changes to the database without committing.

        Use this after creating new Job rows in a batch, so their generated
        ids are available before assigning final slugs.
        """
        self._session.flush()

    def commit(self) -> None:
        """Commit the current transaction.

        Promotion calls this once per batch, not once for the whole run.
        """
        self._session.commit()

    def rollback(self) -> None:
        """Roll back the current transaction.

        Call this in an exception handler to undo all changes made since the
        last commit. The session remains usable after a rollback.
        """
        self._session.rollback()
