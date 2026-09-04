"""Public-facing response models for the job catalogue API.

These models are the contract for anonymous callers, so they deliberately
expose only publishable columns. Internal columns -- ``source_payload``,
``payload_hash``, ``provider_id``, ``source_job_id``/sender reference,
``last_seen_import_run_id`` -- must never be added here.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field


class JobSummary(BaseModel):
    """One job as rendered in a search or listing result."""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    # jobs.id is a bigint identity column; Python ints are unbounded so this
    # maps cleanly. slug is the public identifier used by /jobs/{slug}.
    id: int
    slug: str
    title: str
    advertiser_name: str | None = Field(serialization_alias="company")
    classification: str | None = Field(serialization_alias="category")
    employment_type: str | None
    country_name: str | None
    location: str | None
    apply_url: str = Field(serialization_alias="job_url")
    redirect_url: str | None = None
    first_imported_at: datetime = Field(serialization_alias="posted_date")
    last_imported_at: datetime
    is_active: bool = Field(exclude=True)
    remote_status: str | None = None
    remote_status_source: str | None = None
    experience_level: str | None = None
    experience_level_source: str | None = None

    @computed_field
    @property
    def status(self) -> Literal["active", "expired"]:
        """Translate the internal activity flag into the public lifecycle state."""
        return "active" if self.is_active else "expired"


class JobDetail(JobSummary):
    """The publishable detail view for one active or recently expired job."""

    description: str
    salary_min: Decimal | None
    salary_max: Decimal | None
    salary_currency: str | None
    salary_period: str | None
    created_at: datetime
    source_updated_at: datetime | None = Field(
        default=None,
        validation_alias="content_updated_at",
    )
    structured_data: dict[str, Any] | None


class FilterOptionOut(BaseModel):
    """One populated value in a public job filter."""

    model_config = ConfigDict(from_attributes=True)

    value: str
    count: int


class JobFilterMetadataOut(BaseModel):
    """Available values and active-job counts for each public filter."""

    model_config = ConfigDict(from_attributes=True)

    classifications: list[FilterOptionOut]
    employment_types: list[FilterOptionOut]
    country_names: list[FilterOptionOut]


class JobListResponse(BaseModel):
    """A single keyset-paginated page of job summaries."""

    items: list[JobSummary]
    next_cursor: str | None
    has_more: bool
