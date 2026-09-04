"""Dev-only: seed the jobs table with realistic synthetic Jobg8-shaped data.

Idempotent by clear-and-reseed: every run deletes all previously seeded jobs
(identified by a "SEED-" source_job_id prefix, scoped to the jobg8 provider)
and inserts a fresh batch of --count rows. Running it twice in a row always
leaves exactly --count seeded jobs, never duplicates, and never touches any
job that was not created by this script.

Each row is built by constructing a raw XML-alias dict (the same shape
job_element_to_dict() produces from a real <Job> element) and validating it
through the real JobFeedRecord schema, then computing remote_status and
experience_level with the same inference functions the import pipeline uses.
This is not a simplified approximation: it is the same validation and
inference code a real Jobg8 snapshot goes through, just skipping the
job_staging table and writing straight into jobs.

Refuses to run unless DATABASE_URL points at localhost/127.0.0.1 -- this
script inserts synthetic data and must never touch a shared or production
database.
"""

import argparse
import os
import random
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

# scripts/ is not a package and Python puts this file's own directory on
# sys.path, not the repo root, so `import app...` fails without this. Also
# add scripts/ itself so `from seed_provider import ...` resolves, matching
# the pattern already used by scripts/test_import_cycle.py.
REPO_ROOT = Path(__file__).resolve().parent.parent
for path in (REPO_ROOT, REPO_ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from seed_provider import main as ensure_jobg8_provider
from sqlalchemy import delete, select

from app.core.filters_cache import filters_cache_invalidator
from app.db.models import Job, Provider
from app.db.session import SessionLocal
from app.imports.hashing import compute_payload_hash
from app.imports.promotion import slugify
from app.imports.schemas import JobFeedRecord
from app.services.inference_service import (
    infer_experience_level,
    infer_remote_status,
)

SEED_PREFIX = "SEED-"
_LOCAL_HOSTNAMES = frozenset({"localhost", "127.0.0.1"})

ROLE_TITLES = [
    "Software Engineer",
    "Backend Developer",
    "Frontend Developer",
    "DevOps Engineer",
    "Data Analyst",
    "Data Scientist",
    "Product Manager",
    "Project Manager",
    "UX Designer",
    "Graphic Designer",
    "Marketing Manager",
    "Content Writer",
    "Sales Executive",
    "Account Manager",
    "Customer Support Specialist",
    "HR Business Partner",
    "Recruiter",
    "Accountant",
    "Financial Analyst",
    "Registered Nurse",
    "Medical Assistant",
    "Warehouse Associate",
    "Logistics Coordinator",
    "Store Manager",
    "Retail Associate",
    "Electrician",
    "Mechanical Engineer",
    "Civil Engineer",
    "Teacher",
    "Barista",
    "Chef",
    "Operations Manager",
    "Business Analyst",
    "QA Engineer",
    "Systems Administrator",
]
SENIORITY_PREFIXES = [
    ("", 5),
    ("Junior ", 2),
    ("Senior ", 3),
    ("Lead ", 1),
    ("Principal ", 1),
    ("Entry Level ", 1),
]
COMPANY_PREFIXES = [
    "Bright",
    "Summit",
    "Nova",
    "Blue",
    "North",
    "Silver",
    "River",
    "Cedar",
    "Vertex",
    "Harbor",
    "Union",
    "Granite",
    "Orbit",
    "Maple",
    "Crescent",
]
COMPANY_ROOTS = [
    "Path",
    "Wave",
    "Peak",
    "Field",
    "Works",
    "Bridge",
    "Point",
    "Gate",
    "Line",
    "Stone",
]
COMPANY_SUFFIXES = [
    "Inc.",
    "Group",
    "Partners",
    "Solutions",
    "Technologies",
    "Labs",
    "Co.",
    "Holdings",
]
CLASSIFICATIONS = [
    "Information Technology",
    "Engineering",
    "Healthcare",
    "Sales & Marketing",
    "Finance & Accounting",
    "Customer Service",
    "Education",
    "Construction",
    "Hospitality",
    "Administration",
    "Manufacturing",
    "Logistics & Supply Chain",
]
COUNTRIES = [
    ("United States", 6),
    ("United Kingdom", 3),
    ("Canada", 3),
    ("Australia", 2),
    ("Germany", 1),
    ("Ireland", 1),
    ("New Zealand", 1),
    ("Singapore", 1),
]
CITIES = [
    "Austin, TX",
    "New York, NY",
    "Chicago, IL",
    "Seattle, WA",
    "Denver, CO",
    "London",
    "Manchester",
    "Toronto, ON",
    "Vancouver, BC",
    "Sydney",
    "Melbourne",
    "Berlin",
    "Dublin",
    "Auckland",
    "Singapore",
    "Remote",
]
EMPLOYMENT_TYPE_RAW_VALUES = [
    "Full Time",
    "Part Time",
    "Contract",
    "Contractor",
    "Temporary",
    "Internship",
    "Permanent",
    "Freelance",
]
ADVERTISER_TYPES = ["Direct Employer", "Agency", "Recruiter"]
DURATIONS = ["Permanent", "6 Month Contract", "12 Month Contract", "Temporary"]
WORK_HOURS = ["Full Time", "Part Time"]
START_DATE_TEXT = ["Immediate", "ASAP", "Flexible", "2026-10-01"]
CURRENCY_NAMES = {
    "USD": "US Dollar",
    "GBP": "British Pound",
    "EUR": "Euro",
    "CAD": "Canadian Dollar",
    "AUD": "Australian Dollar",
}
SALARY_PERIODS = ["Annual", "Monthly", "Hourly"]
REMOTE_TITLE_SUFFIXES = [(" - Remote", 3), (" - Hybrid", 2), ("", 10)]
REMOTE_DESCRIPTION_CLAUSES = [
    "This is a fully remote position.",
    "This is a hybrid role requiring occasional office visits.",
    "This is an onsite position based in our office.",
    None,
    None,
    None,
]
DESCRIPTION_TEMPLATES = [
    (
        "{company} is looking for a {title} to join our growing team. "
        "You will work closely with cross-functional stakeholders to deliver "
        "high-quality results and help shape how we operate. "
        "We value collaboration, ownership, and clear communication. {remote_clause}"
    ),
    (
        "As a {title} at {company}, you'll take ownership of key projects from "
        "day one. We're a fast-moving team that cares about doing the work "
        "right, not just fast. Competitive benefits and a supportive culture "
        "included. {remote_clause}"
    ),
    (
        "{company} is hiring a {title}. In this role you'll partner with "
        "experienced colleagues, contribute to planning, and help us serve our "
        "customers better every day. Prior experience in a similar role is a "
        "plus but not required. {remote_clause}"
    ),
]


def _weighted_choice(options: list[tuple[str, int]]) -> str:
    values = [value for value, _ in options]
    weights = [weight for _, weight in options]
    return random.choices(values, weights=weights, k=1)[0]


def _refuse_unless_local_database() -> None:
    """Hard-refuse to run against anything that is not an obvious local DB.

    This script inserts hundreds of synthetic rows and deletes/reseeds them
    on every run -- it must never be pointed at a shared or production
    database, so a comment alone is not enough of a guard.
    """
    database_url = os.environ.get("DATABASE_URL", "")
    hostname = urlsplit(database_url).hostname
    if hostname not in _LOCAL_HOSTNAMES:
        raise SystemExit(
            "Refusing to seed: DATABASE_URL does not look like a local "
            f"database (host={hostname!r}). This script only ever runs "
            "against a local dev database, e.g. "
            "postgresql://postgres:password@localhost:5432/job_board."
        )


def _build_company_name() -> str:
    return (
        f"{random.choice(COMPANY_PREFIXES)}{random.choice(COMPANY_ROOTS)} "
        f"{random.choice(COMPANY_SUFFIXES)}"
    )


def _build_title(role: str) -> str:
    prefix = _weighted_choice(SENIORITY_PREFIXES)
    suffix = _weighted_choice(REMOTE_TITLE_SUFFIXES)
    return f"{prefix}{role}{suffix}"


def _build_description(title: str, company: str) -> str:
    template = random.choice(DESCRIPTION_TEMPLATES)
    remote_clause = random.choice(REMOTE_DESCRIPTION_CLAUSES) or ""
    return template.format(
        title=title, company=company, remote_clause=remote_clause
    ).strip()


def _build_salary_fields() -> tuple[str | None, str | None, str | None, str | None]:
    """Return (min, max, currency_raw, period) as raw feed-style strings."""
    if random.random() < 0.15:
        return None, None, None, None

    period = random.choice(SALARY_PERIODS)
    if period == "Annual":
        low = random.randint(40_000, 90_000)
        high = low + random.randint(5_000, 60_000)
    elif period == "Monthly":
        low = random.randint(3_000, 9_000)
        high = low + random.randint(500, 4_000)
    else:
        low = random.randint(15, 90)
        high = low + random.randint(2, 40)

    code = random.choice(list(CURRENCY_NAMES))
    if random.random() < 0.15:
        currency_raw = f"{CURRENCY_NAMES[code]} . {code}"
    else:
        currency_raw = code

    return f"{low:.2f}", f"{high:.2f}", currency_raw, period


def _build_raw_record(index: int) -> dict[str, str | None]:
    """Build one raw feed-shaped record, keyed by XML element name (alias)."""
    role = random.choice(ROLE_TITLES)
    title = _build_title(role)
    company = _build_company_name()
    country = _weighted_choice(COUNTRIES)
    salary_min, salary_max, salary_currency, salary_period = _build_salary_fields()

    return {
        "SenderReference": f"{SEED_PREFIX}{index:06d}",
        "AdvertiserName": company,
        "AdvertiserType": random.choice(ADVERTISER_TYPES),
        "DisplayReference": f"REF-{index:06d}",
        "Classification": random.choice(CLASSIFICATIONS),
        "Position": title,
        "Description": _build_description(title, company),
        "Country": country,
        "Location": random.choice(CITIES),
        "Area": None,
        "PostalCode": None,
        "ApplicationURL": f"https://careers.example.test/jobs/{index:06d}",
        "Language": "1033",
        "EmploymentType": random.choice(EMPLOYMENT_TYPE_RAW_VALUES),
        "StartDate": random.choice(START_DATE_TEXT),
        "Duration": random.choice(DURATIONS),
        "WorkHours": random.choice(WORK_HOURS),
        "SalaryCurrency": salary_currency,
        "SalaryMinimum": salary_min,
        "SalaryMaximum": salary_max,
        "SalaryPeriod": salary_period,
        "SalaryAdditional": None,
        "LogoURL": None,
        "JobType": random.choice(["TRAFFIC", "ATS"]),
        "SellPrice": None,
        "SellPriceCurrency": None,
        "RevenueType": None,
    }


def _build_job(provider: Provider, index: int, now: datetime) -> Job:
    """Validate one synthetic record through the real feed schema and build a Job."""
    record = JobFeedRecord.model_validate(_build_raw_record(index))
    payload_hash = compute_payload_hash(record)
    remote_status, remote_status_source = infer_remote_status(
        record.title, record.description
    )
    experience_level, experience_level_source = infer_experience_level(
        record.title, record.description
    )

    first_imported_at = now - timedelta(
        days=random.randint(0, 30), hours=random.randint(0, 23)
    )
    last_imported_at = min(
        first_imported_at + timedelta(hours=random.randint(0, 72)), now
    )

    is_active = random.random() < 0.85
    deactivated_at = None
    if not is_active:
        deactivated_at = min(
            last_imported_at + timedelta(hours=random.randint(1, 48)), now
        )

    return Job(
        source_name=provider.name,
        provider_id=provider.id,
        source_job_id=record.sender_reference,
        slug=f"__pending__seed__{index}",
        advertiser_name=record.advertiser_name,
        advertiser_type=record.advertiser_type,
        display_reference=record.display_reference,
        classification=record.classification,
        title=record.title,
        description=record.description,
        country_name=record.country_name,
        location=record.location,
        area=record.area,
        postal_code=record.postal_code,
        apply_url=record.apply_url,
        language_code=record.language_code,
        employment_type=record.employment_type,
        start_date_text=record.start_date_text,
        duration=record.duration,
        work_hours=record.work_hours,
        salary_currency=record.salary_currency,
        salary_min=record.salary_min,
        salary_max=record.salary_max,
        salary_period=record.salary_period,
        salary_additional=record.salary_additional,
        advertiser_logo_url=record.advertiser_logo_url,
        job_type=record.job_type,
        remote_status=remote_status,
        remote_status_source=remote_status_source,
        experience_level=experience_level,
        experience_level_source=experience_level_source,
        source_payload=record.source_record,
        payload_hash=payload_hash,
        last_seen_import_run_id=None,
        is_active=is_active,
        first_imported_at=first_imported_at,
        last_imported_at=last_imported_at,
        deactivated_at=deactivated_at,
        created_at=first_imported_at,
        updated_at=last_imported_at,
        content_updated_at=last_imported_at,
    )


def seed(count: int) -> None:
    ensure_jobg8_provider()

    with SessionLocal() as session:
        provider = session.scalar(select(Provider).where(Provider.name == "jobg8"))
        if provider is None:
            raise SystemExit("Provider 'jobg8' still missing after ensure step")

        deleted = session.execute(
            delete(Job).where(
                Job.provider_id == provider.id,
                Job.source_job_id.like(f"{SEED_PREFIX}%"),
            )
        )
        print(f"Removed {deleted.rowcount} previously seeded job(s)")

        now = datetime.now(UTC)
        jobs = [_build_job(provider, index, now) for index in range(count)]
        session.add_all(jobs)
        session.flush()

        for job in jobs:
            slug_base = slugify(f"{job.title}-{job.advertiser_name or ''}")
            job.slug = f"{slug_base}-{job.id}"

        session.commit()

        # Mirrors PromotionService: the public /jobs/filters response is
        # served from a versioned Redis cache, so without this bump it would
        # keep serving pre-seed (or previous seed run) facet values for up to
        # FILTERS_CACHE_TTL_SECONDS.
        filters_cache_invalidator.bump_version()

        active_count = sum(1 for job in jobs if job.is_active)
        print(
            f"Inserted {len(jobs)} seeded job(s): {active_count} active, "
            f"{len(jobs) - active_count} expired"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Seed the jobs table with realistic synthetic Jobg8-shaped data "
            "for local frontend/API development. Idempotent by "
            "clear-and-reseed: every run deletes all jobs it previously "
            "created (source_job_id prefixed 'SEED-', scoped to the jobg8 "
            "provider) and inserts a fresh --count batch. Never touches "
            "jobs from a real import. Refuses to run unless DATABASE_URL "
            "points at localhost/127.0.0.1."
        )
    )
    parser.add_argument(
        "--count",
        type=int,
        default=200,
        help="Number of synthetic jobs to generate (default: 200)",
    )
    args = parser.parse_args()
    if args.count < 1:
        raise SystemExit("--count must be at least 1")

    _refuse_unless_local_database()
    seed(args.count)


if __name__ == "__main__":
    main()
