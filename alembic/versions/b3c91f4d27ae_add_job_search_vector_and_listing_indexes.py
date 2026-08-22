"""Add job search vector and public listing indexes

Revision ID: b3c91f4d27ae
Revises: ae7ed75aa030
Create Date: 2026-08-22

Adds the full-text search source column and the two indexes the public
``GET /api/v1/jobs`` listing depends on:

* ``jobs.search_vector`` -- a PostgreSQL ``GENERATED ALWAYS ... STORED``
  tsvector over title and description. A generated column (rather than a bare
  expression index) is used so the stored value can later back ranking and
  highlighting without recomputing ``to_tsvector`` per row, and so the value
  can never drift from the columns it is derived from.
* ``jobs_search_vector_gin_idx`` -- GIN index backing the ``q`` filter.
* ``jobs_active_last_imported_at_id_idx`` -- composite index matching the
  listing's ``is_active`` equality plus its keyset ordering. Both key columns
  descend so that ONE index serves both sort directions: a forward scan yields
  ``(last_imported_at DESC, id DESC)`` and a backward scan yields
  ``(last_imported_at ASC, id ASC)``. A mixed-direction index (``id ASC``
  here) would force a sort on the ascending page and, worse, would stop the
  DESC cursor predicate from being expressible as a row comparison -- turning
  it into a Filter that rescans from the top of the index on every page.

NOTE: adding a STORED generated column rewrites the ``jobs`` table and holds an
ACCESS EXCLUSIVE lock for the duration. On the current ~335k row table that is
a matter of seconds, but treat it as a maintenance operation.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import TSVECTOR

from alembic import op

revision: str = "b3c91f4d27ae"
down_revision: str | Sequence[str] | None = "ae7ed75aa030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEARCH_VECTOR_EXPRESSION = (
    "setweight(to_tsvector('english', coalesce(title, '')), 'A') || "
    "setweight(to_tsvector('english', coalesce(description, '')), 'B')"
)


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column(
            "search_vector",
            TSVECTOR(),
            sa.Computed(SEARCH_VECTOR_EXPRESSION, persisted=True),
            nullable=True,
        ),
    )
    op.create_index(
        "jobs_search_vector_gin_idx",
        "jobs",
        ["search_vector"],
        postgresql_using="gin",
    )
    op.create_index(
        "jobs_active_last_imported_at_id_idx",
        "jobs",
        ["is_active", sa.text("last_imported_at DESC"), sa.text("id DESC")],
    )


def downgrade() -> None:
    op.drop_index("jobs_active_last_imported_at_id_idx", table_name="jobs")
    op.drop_index("jobs_search_vector_gin_idx", table_name="jobs")
    op.drop_column("jobs", "search_vector")
