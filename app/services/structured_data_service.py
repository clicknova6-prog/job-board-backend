"""Build schema.org structured data for public job detail responses."""

from __future__ import annotations

from typing import Any

from app.db.public_job_repositories import JobDetailRecord
from app.services.schema_mappings import (
    schema_employment_type,
    schema_salary_period,
)


def _present(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def build_job_posting_ld(job: JobDetailRecord) -> dict[str, Any] | None:
    """Return ready-to-embed JobPosting JSON-LD for an active job."""
    if not job.is_active:
        return None

    posting: dict[str, Any] = {
        "@context": "https://schema.org/",
        "@type": "JobPosting",
        "title": job.title,
        "description": job.description,
        "identifier": {
            "@type": "PropertyValue",
            "name": "job-board",
            "value": str(job.id),
        },
        "datePosted": job.first_imported_at.isoformat(),
        "directApply": True,
        "url": job.apply_url,
    }

    employment_type = schema_employment_type(job.employment_type)
    if employment_type is not None:
        posting["employmentType"] = employment_type

    advertiser_name = _present(job.advertiser_name)
    if advertiser_name is not None:
        posting["hiringOrganization"] = {
            "@type": "Organization",
            "name": advertiser_name,
        }

    address_fields = {
        key: present
        for key, value in (
            ("addressLocality", job.location),
            ("addressRegion", job.area),
            ("postalCode", job.postal_code),
            ("addressCountry", job.country_name),
        )
        if (present := _present(value)) is not None
    }
    if address_fields:
        posting["jobLocation"] = {
            "@type": "Place",
            "address": {"@type": "PostalAddress", **address_fields},
        }

    if job.salary_min is not None or job.salary_max is not None:
        salary_value: dict[str, Any] = {"@type": "QuantitativeValue"}
        if job.salary_min is not None:
            salary_value["minValue"] = job.salary_min
        if job.salary_max is not None:
            salary_value["maxValue"] = job.salary_max
        unit_text = schema_salary_period(job.salary_period)
        if unit_text is not None:
            salary_value["unitText"] = unit_text

        base_salary: dict[str, Any] = {
            "@type": "MonetaryAmount",
            "value": salary_value,
        }
        salary_currency = _present(job.salary_currency)
        if salary_currency is not None:
            base_salary["currency"] = salary_currency
        posting["baseSalary"] = base_salary

    return posting
