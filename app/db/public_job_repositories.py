"""Async read-side persistence for the public job catalogue API.

Mirrors ``app.db.auth_repositories``: ORM objects stay inside this module and
callers receive plain frozen dataclasses. ``app.db.repositories`` remains the
synchronous import/promotion pipeline's repository module.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Select, func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Job

# Must match the regconfig baked into the jobs.search_vector generated column
# (see alembic revision b3c91f4d27ae); a mismatch silently returns no rows.
SEARCH_TEXT_CONFIG = "english"


@dataclass(frozen=True, slots=True)
class JobSummaryRecord:
    """One publishable job row handed out of the repository layer."""

    id: int
    slug: str
    title: str
    classification: str | None
    employment_type: str | None
    country_name: str | None
    location: str | None
    apply_url: str
    last_imported_at: datetime


@dataclass(frozen=True, slots=True)
class JobSearchFilters:
    """Equality and full-text predicates for a public job search."""

    classification: str | None = None
    employment_type: str | None = None
    country_name: str | None = None
    location: str | None = None
    q: str | None = None


@dataclass(frozen=True, slots=True)
class JobKeysetCursor:
    """The ``(last_imported_at, id)`` position a page resumes after."""

    last_imported_at: datetime
    id: int


class PublicJobRepository:
    """Reads active jobs for anonymous catalogue browsing."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def search(
        self,
        *,
        filters: JobSearchFilters,
        descending: bool,
        limit: int,
        cursor: JobKeysetCursor | None,
    ) -> list[JobSummaryRecord]:
        """Return at most ``limit`` active jobs after ``cursor``.

        Callers requesting ``limit + 1`` rows use the extra row to decide
        ``has_more`` without a second count query.
        """
        statement = self._apply_filters(
            select(
                Job.id,
                Job.slug,
                Job.title,
                Job.classification,
                Job.employment_type,
                Job.country_name,
                Job.location,
                Job.apply_url,
                Job.last_imported_at,
            ).where(Job.is_active.is_(True)),
            filters,
        )

        # The id tiebreaker follows the primary sort direction, which keeps the
        # cursor predicate a true row comparison in BOTH directions. PostgreSQL
        # turns a row comparison into an Index Cond on
        # jobs_active_last_imported_at_id_idx, so a page costs the same at any
        # depth. Splitting the directions (id always ASC) would demote the DESC
        # predicate to a Filter that rescans from the top of the index -- ~285ms
        # and 200k discarded rows by page 10,000 on the live table, because a
        # feed import stamps every row with the same last_imported_at.
        if cursor is not None:
            cursor_row = tuple_(cursor.last_imported_at, cursor.id)
            row = tuple_(Job.last_imported_at, Job.id)
            statement = statement.where(
                row < cursor_row if descending else row > cursor_row
            )

        if descending:
            statement = statement.order_by(Job.last_imported_at.desc(), Job.id.desc())
        else:
            statement = statement.order_by(Job.last_imported_at.asc(), Job.id.asc())
        statement = statement.limit(limit)

        result = await self._session.execute(statement)
        return [JobSummaryRecord(**row) for row in result.mappings()]

    @staticmethod
    def _apply_filters(statement: Select, filters: JobSearchFilters) -> Select:
        """Add the optional equality and full-text predicates to ``statement``."""
        equality_columns = (
            (Job.classification, filters.classification),
            (Job.employment_type, filters.employment_type),
            (Job.country_name, filters.country_name),
            (Job.location, filters.location),
        )
        for column, value in equality_columns:
            if value is not None:
                statement = statement.where(column == value)

        if filters.q is not None:
            # websearch_to_tsquery never raises on malformed user input (unlike
            # to_tsquery), so arbitrary search boxes cannot produce a 500.
            statement = statement.where(
                Job.search_vector.op("@@")(
                    func.websearch_to_tsquery(SEARCH_TEXT_CONFIG, filters.q)
                )
            )

        return statement
