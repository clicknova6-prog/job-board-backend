"""SQLAlchemy mappings for the finalized PostgreSQL job-import schema."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum as PyEnum
from typing import Any
from uuid import UUID as UUIDValue

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class OAuthProvider(str, PyEnum):
    """Supported OAuth identity providers for job seekers."""

    GOOGLE = "google"
    APPLE = "apple"


class AdminRole(str, PyEnum):
    """Administrative authorization roles."""

    ADMIN = "admin"
    SUPER_ADMIN = "super_admin"


class Provider(Base):
    """A configured upstream job feed source (e.g. Jobg8)."""

    __tablename__ = "providers"
    __table_args__ = (
        CheckConstraint(
            "schedule_interval_minutes IS NULL OR schedule_interval_minutes > 0",
            name="providers_schedule_interval_positive_check",
        ),
        CheckConstraint(
            "deleted_job_retention_hours IS NULL OR deleted_job_retention_hours > 0",
            name="providers_deleted_job_retention_positive_check",
        ),
        {"comment": "Configured upstream job feed sources."},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    feed_url: Mapped[str | None] = mapped_column(
        Text,
        comment="SSRF validation happens at the application layer, not here.",
    )
    format: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'xml'")
    )
    archive_type: Mapped[str | None] = mapped_column(Text)
    schedule_cron: Mapped[str | None] = mapped_column(Text)
    schedule_interval_minutes: Mapped[int | None] = mapped_column(Integer)
    deleted_job_retention_hours: Mapped[int | None] = mapped_column(
        Integer, server_default=text("12")
    )
    timeout_seconds: Mapped[int | None] = mapped_column(Integer)
    retry_max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("3")
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
        comment="Provider-specific settings and credential references only; never raw secrets.",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ImportRun(Base):
    """One attempt to process a complete hourly Jobg8 feed snapshot."""

    __tablename__ = "import_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'failed')",
            name="import_runs_status_check",
        ),
        CheckConstraint(
            "records_received >= 0 AND records_staged >= 0 "
            "AND records_imported >= 0 AND records_rejected >= 0 "
            "AND new_jobs >= 0 AND updated_jobs >= 0 AND deleted_jobs >= 0",
            name="import_runs_counts_check",
        ),
        CheckConstraint(
            "completed_at IS NULL OR started_at IS NULL OR completed_at >= started_at",
            name="import_runs_completed_after_started_check",
        ),
        Index("import_runs_status_created_at_idx", "status", text("created_at DESC")),
        {"comment": "One record per hourly Jobg8 feed import attempt."},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    source_name: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Configured provider name; it is operational metadata, not an XML field.",
    )
    provider_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("providers.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_uri: Mapped[str | None] = mapped_column(Text)
    source_checksum: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'pending'")
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    records_received: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    records_staged: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    records_imported: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    records_rejected: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    new_jobs: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    updated_jobs: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    deleted_jobs: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    unmapped_fields: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
        comment="Unrecognized feed element names seen this run, mapped to occurrence counts.",
    )
    field_fallback_warnings: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
        comment="Field names that required fallback handling, mapped to record counts.",
    )
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # A run owns staging rows, while jobs only reference the run that last saw
    # them. The separate relationships make both foreign keys unambiguous.
    staged_jobs: Mapped[list[JobStaging]] = relationship(
        back_populates="import_run",
        cascade="all, delete-orphan",
        passive_deletes=True,
        foreign_keys="JobStaging.import_run_id",
    )
    last_seen_jobs: Mapped[list[Job]] = relationship(
        back_populates="last_seen_import_run",
        foreign_keys="Job.last_seen_import_run_id",
    )


class JobStaging(Base):
    """A parsed source record and its validation state for an import run."""

    __tablename__ = "job_staging"
    __table_args__ = (
        CheckConstraint(
            "jsonb_typeof(raw_payload) = 'object'",
            name="job_staging_payload_object_check",
        ),
        CheckConstraint(
            "jsonb_typeof(validation_errors) = 'array'",
            name="job_staging_validation_errors_array_check",
        ),
        CheckConstraint(
            "salary_min IS NULL OR salary_max IS NULL OR salary_min <= salary_max",
            name="job_staging_salary_range_check",
        ),
        UniqueConstraint(
            "import_run_id",
            "source_job_id",
            name="job_staging_import_run_source_job_unique",
        ),
        Index("job_staging_import_run_valid_idx", "import_run_id", "is_valid"),
        {"comment": "Parsed records before promotion into the canonical jobs table."},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    import_run_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("import_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_job_id: Mapped[str] = mapped_column(Text, nullable=False)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        comment="Complete source record, including provider-only and future fields.",
    )
    payload_hash: Mapped[str] = mapped_column(Text, nullable=False)

    advertiser_name: Mapped[str | None] = mapped_column(Text)
    advertiser_type: Mapped[str | None] = mapped_column(Text)
    display_reference: Mapped[str | None] = mapped_column(Text)
    classification: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    country_name: Mapped[str | None] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text)
    area: Mapped[str | None] = mapped_column(Text)
    postal_code: Mapped[str | None] = mapped_column(Text)
    apply_url: Mapped[str | None] = mapped_column(Text)
    language_code: Mapped[str | None] = mapped_column(Text)
    employment_type: Mapped[str | None] = mapped_column(Text)
    start_date_text: Mapped[str | None] = mapped_column(Text)
    duration: Mapped[str | None] = mapped_column(Text)
    work_hours: Mapped[str | None] = mapped_column(Text)
    salary_currency: Mapped[str | None] = mapped_column(String(3))
    salary_min: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    salary_max: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    salary_period: Mapped[str | None] = mapped_column(Text)
    salary_additional: Mapped[str | None] = mapped_column(Text)
    advertiser_logo_url: Mapped[str | None] = mapped_column(Text)
    job_type: Mapped[str | None] = mapped_column(Text)

    validation_errors: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'[]'::jsonb"),
    )
    is_valid: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    staged_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    import_run: Mapped[ImportRun] = relationship(
        back_populates="staged_jobs",
        foreign_keys=[import_run_id],
    )


class Job(Base):
    """Current canonical job row from the latest successful feed snapshots."""

    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint(
            "source_name", "source_job_id", name="jobs_source_identity_unique"
        ),
        UniqueConstraint(
            "provider_id", "source_job_id", name="jobs_provider_source_job_unique"
        ),
        UniqueConstraint("slug", name="jobs_slug_unique"),
        CheckConstraint(
            "jsonb_typeof(source_payload) = 'object'",
            name="jobs_source_payload_object_check",
        ),
        CheckConstraint("btrim(title) <> ''", name="jobs_title_not_blank_check"),
        CheckConstraint(
            "btrim(description) <> ''", name="jobs_description_not_blank_check"
        ),
        CheckConstraint("apply_url ~* '^https?://'", name="jobs_apply_url_http_check"),
        CheckConstraint(
            "salary_min IS NULL OR salary_max IS NULL OR salary_min <= salary_max",
            name="jobs_salary_range_check",
        ),
        CheckConstraint(
            "last_imported_at >= first_imported_at", name="jobs_import_timestamps_check"
        ),
        CheckConstraint(
            "(is_active = true AND deactivated_at IS NULL) "
            "OR (is_active = false AND deactivated_at IS NOT NULL)",
            name="jobs_active_deactivated_consistency_check",
        ),
        Index("jobs_last_seen_import_run_id_idx", "last_seen_import_run_id"),
        Index("jobs_provider_id_idx", "provider_id"),
        Index(
            "jobs_active_classification_idx",
            "classification",
            postgresql_where=text("is_active = true"),
        ),
        Index(
            "jobs_active_country_name_idx",
            "country_name",
            postgresql_where=text("is_active = true"),
        ),
        # Serves the public listing's keyset page. Both key columns descend so
        # a forward scan gives (last_imported_at DESC, id DESC) and a backward
        # scan gives (last_imported_at ASC, id ASC) -- one index, both sort
        # directions, and the cursor predicate stays a row comparison.
        Index(
            "jobs_active_last_imported_at_id_idx",
            "is_active",
            text("last_imported_at DESC"),
            text("id DESC"),
        ),
        Index("jobs_search_vector_gin_idx", "search_vector", postgresql_using="gin"),
        {"comment": "Current canonical job catalogue from Jobg8 snapshots."},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    source_name: Mapped[str] = mapped_column(Text, nullable=False)
    provider_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("providers.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_job_id: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Jobg8 SenderReference stored as the source-level job identity.",
    )
    slug: Mapped[str] = mapped_column(Text, nullable=False)

    advertiser_name: Mapped[str | None] = mapped_column(Text)
    advertiser_type: Mapped[str | None] = mapped_column(Text)
    display_reference: Mapped[str | None] = mapped_column(Text)
    classification: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    country_name: Mapped[str | None] = mapped_column(
        Text,
        comment="Feed country name; the source does not supply an ISO country code.",
    )
    location: Mapped[str | None] = mapped_column(
        Text,
        comment="Free-form feed location; may be Remote rather than a city.",
    )
    area: Mapped[str | None] = mapped_column(Text)
    postal_code: Mapped[str | None] = mapped_column(Text)
    apply_url: Mapped[str] = mapped_column(Text, nullable=False)
    language_code: Mapped[str | None] = mapped_column(Text)
    employment_type: Mapped[str | None] = mapped_column(Text)
    start_date_text: Mapped[str | None] = mapped_column(Text)
    duration: Mapped[str | None] = mapped_column(Text)
    work_hours: Mapped[str | None] = mapped_column(Text)
    salary_currency: Mapped[str | None] = mapped_column(String(3))
    salary_min: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    salary_max: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    salary_period: Mapped[str | None] = mapped_column(Text)
    salary_additional: Mapped[str | None] = mapped_column(Text)
    advertiser_logo_url: Mapped[str | None] = mapped_column(Text)
    job_type: Mapped[str | None] = mapped_column(Text)

    # Full-text search index source. Generated by PostgreSQL so it can never
    # drift from title/description, weighted A/B so title matches can be
    # ranked above body matches once ranking is needed.
    search_vector: Mapped[str | None] = mapped_column(
        TSVECTOR,
        Computed(
            "setweight(to_tsvector('english', coalesce(title, '')), 'A') || "
            "setweight(to_tsvector('english', coalesce(description, '')), 'B')",
            persisted=True,
        ),
    )

    # Provider commercial values stay inside this JSONB payload by design.
    source_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    payload_hash: Mapped[str] = mapped_column(Text, nullable=False)
    last_seen_import_run_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("import_runs.id", ondelete="RESTRICT"),
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    first_imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    last_seen_import_run: Mapped[ImportRun | None] = relationship(
        back_populates="last_seen_jobs",
        foreign_keys=[last_seen_import_run_id],
    )
    provider: Mapped[Provider] = relationship(foreign_keys=[provider_id])


class User(Base):
    """A job seeker authenticated through an OAuth provider."""

    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint(
            "oauth_provider",
            "oauth_subject_id",
            name="users_oauth_identity_unique",
        ),
        Index("users_email_unique_idx", "email", unique=True),
        {"comment": "OAuth-authenticated job-seeker accounts."},
    )

    id: Mapped[UUIDValue] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    email: Mapped[str] = mapped_column(Text, nullable=False)
    oauth_provider: Mapped[OAuthProvider] = mapped_column(
        SQLEnum(
            OAuthProvider,
            name="oauth_provider_enum",
            values_callable=lambda enum_type: [member.value for member in enum_type],
            validate_strings=True,
        ),
        nullable=False,
    )
    oauth_subject_id: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    refresh_tokens: Mapped[list[RefreshToken]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
        foreign_keys="RefreshToken.user_id",
    )


class AdminUser(Base):
    """A separately authenticated administrative account."""

    __tablename__ = "admin_users"
    __table_args__ = (
        UniqueConstraint("email", name="admin_users_email_unique"),
        {"comment": "Password-authenticated administrative accounts."},
    )

    id: Mapped[UUIDValue] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    email: Mapped[str] = mapped_column(Text, nullable=False)
    password_hash: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Argon2 password hash; plaintext passwords are never stored.",
    )
    role: Mapped[AdminRole] = mapped_column(
        SQLEnum(
            AdminRole,
            name="admin_role_enum",
            values_callable=lambda enum_type: [member.value for member in enum_type],
            validate_strings=True,
        ),
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    refresh_tokens: Mapped[list[RefreshToken]] = relationship(
        back_populates="admin_user",
        cascade="all, delete-orphan",
        passive_deletes=True,
        foreign_keys="RefreshToken.admin_user_id",
    )


class RefreshToken(Base):
    """A hashed refresh token belonging to exactly one account type."""

    __tablename__ = "refresh_tokens"
    __table_args__ = (
        CheckConstraint(
            "(user_id IS NOT NULL AND admin_user_id IS NULL) "
            "OR (user_id IS NULL AND admin_user_id IS NOT NULL)",
            name="refresh_tokens_exactly_one_owner_check",
        ),
        Index("refresh_tokens_token_hash_idx", "token_hash"),
        Index("refresh_tokens_expires_at_idx", "expires_at"),
        Index("refresh_tokens_user_id_idx", "user_id"),
        Index("refresh_tokens_admin_user_id_idx", "admin_user_id"),
        {"comment": "Hashed refresh-token state; raw tokens are never stored."},
    )

    id: Mapped[UUIDValue] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    token_hash: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="One-way hash of the refresh token; never the raw token.",
    )
    user_id: Mapped[UUIDValue | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
    )
    admin_user_id: Mapped[UUIDValue | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("admin_users.id", ondelete="CASCADE"),
    )
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User | None] = relationship(
        back_populates="refresh_tokens",
        foreign_keys=[user_id],
    )
    admin_user: Mapped[AdminUser | None] = relationship(
        back_populates="refresh_tokens",
        foreign_keys=[admin_user_id],
    )
