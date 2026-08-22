"""Output-only mappings from stored feed labels to schema.org enums."""

from __future__ import annotations

import re

_SEPARATORS = re.compile(r"[\s_-]+")

_EMPLOYMENT_TYPE_MAP = {
    "full time": "FULL_TIME",
    "part time": "PART_TIME",
    "contract": "CONTRACTOR",
    "contractor": "CONTRACTOR",
    "temporary": "TEMPORARY",
    "temp": "TEMPORARY",
    "intern": "INTERN",
    "internship": "INTERN",
    "volunteer": "VOLUNTEER",
    "per diem": "PER_DIEM",
    "other": "OTHER",
}

_SALARY_PERIOD_MAP = {
    "hour": "HOUR",
    "hourly": "HOUR",
    "day": "DAY",
    "daily": "DAY",
    "week": "WEEK",
    "weekly": "WEEK",
    "month": "MONTH",
    "monthly": "MONTH",
    "year": "YEAR",
    "yearly": "YEAR",
    "annual": "YEAR",
    "annually": "YEAR",
}


def _lookup(value: str | None, mapping: dict[str, str]) -> str | None:
    if value is None:
        return None
    key = _SEPARATORS.sub(" ", value.strip().casefold())
    return mapping.get(key)


def schema_employment_type(value: str | None) -> str | None:
    """Map a known stored employment label without changing stored data."""
    return _lookup(value, _EMPLOYMENT_TYPE_MAP)


def schema_salary_period(value: str | None) -> str | None:
    """Map a known stored salary period without changing stored data."""
    return _lookup(value, _SALARY_PERIOD_MAP)
