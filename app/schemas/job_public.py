"""Public-facing response models for the job catalogue API.

These models are the contract for anonymous callers, so they deliberately
expose only publishable columns. Internal columns -- ``source_payload``,
``payload_hash``, ``provider_id``, ``source_job_id``/sender reference,
``last_seen_import_run_id`` -- must never be added here.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class JobSummary(BaseModel):
    """One job as rendered in a search or listing result."""

    model_config = ConfigDict(from_attributes=True)

    # jobs.id is a bigint identity column; Python ints are unbounded so this
    # maps cleanly. slug is the public identifier used by /jobs/{slug}.
    id: int
    slug: str
    title: str
    classification: str | None
    employment_type: str | None
    country_name: str | None
    location: str | None
    apply_url: str
    last_imported_at: datetime


class JobListResponse(BaseModel):
    """A single keyset-paginated page of job summaries."""

    items: list[JobSummary]
    next_cursor: str | None
    has_more: bool
