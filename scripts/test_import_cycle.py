"""Manual dev script: run one full staging -> promotion cycle end to end.

Not a pytest test. This drives the real services against a real database
so the whole pipeline can be eyeballed after a schema or logic change.

Requires DATABASE_URL to be exported (app.db.session reads os.environ
directly; the repo's .env file is not loaded by anything).
"""

import sys
from pathlib import Path

# scripts/ is not a package and Python puts this file's own directory on
# sys.path, not the repo root, so `import app...` fails without this.
REPO_ROOT = Path(__file__).resolve().parent.parent
for path in (REPO_ROOT, REPO_ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from seed_provider import main as ensure_jobg8_provider
from sqlalchemy import func, select

from app.db.models import ImportRun, Job
from app.db.session import SessionLocal
from app.imports.importer import ImportService
from app.imports.promotion import PromotionService

FEED_PATH = REPO_ROOT / "tests" / "fixtures" / "sample_feed.xml"


def main() -> None:
    if not FEED_PATH.exists():
        raise SystemExit(
            f"Feed fixture not found: {FEED_PATH}\n"
            "Generate it first with: python -m scripts.extract_sample <feed.zip>"
        )

    print("=" * 60)
    print("STEP 1: ensure jobg8 provider exists")
    print("=" * 60)
    ensure_jobg8_provider()

    print()
    print("=" * 60)
    print("STEP 2: staging (ImportService)")
    print("=" * 60)
    print(f"feed: {FEED_PATH}")
    import_summary = ImportService(FEED_PATH).run()
    print(
        f"staged: total={import_summary.total_jobs} "
        f"valid={import_summary.valid_jobs} invalid={import_summary.invalid_jobs}"
    )

    run_id = import_summary.import_run_id

    print()
    print("=" * 60)
    print("STEP 3: promotion (PromotionService)")
    print("=" * 60)
    print(f"import_run id: {run_id}")
    promotion_summary = PromotionService(run_id).run()

    print()
    print("=" * 60)
    print("STEP 4: import_run summary")
    print("=" * 60)
    with SessionLocal() as session:
        run = session.get(ImportRun, run_id)
        print(f"id               : {run.id}")
        print(f"status           : {run.status}")
        print(f"provider_id      : {run.provider_id}")
        print(f"records_received : {run.records_received}")
        print(f"records_staged   : {run.records_staged}")
        print(f"records_rejected : {run.records_rejected}")
        print(f"records_imported : {run.records_imported}")
        print(f"new_jobs         : {run.new_jobs}")
        print(f"updated_jobs     : {run.updated_jobs}")
        print(f"deleted_jobs     : {run.deleted_jobs}")
        if run.error_message:
            print(f"error_message    : {run.error_message}")

    print()
    print("promotion counts returned by the service:")
    print(f"  new_jobs         : {promotion_summary.new_jobs}")
    print(f"  updated_jobs     : {promotion_summary.updated_jobs}")
    print(f"  unchanged_jobs   : {promotion_summary.unchanged_jobs}")
    print(f"  deactivated_jobs : {promotion_summary.deactivated_jobs}")

    print()
    print("=" * 60)
    print("STEP 5: jobs table sanity check")
    print("=" * 60)
    with SessionLocal() as session:
        total_count = session.scalar(select(func.count()).select_from(Job))
        print(f"total rows in jobs: {total_count}")
        first_three = list(session.scalars(select(Job).order_by(Job.id).limit(3)))
        for job in first_three:
            print(
                f"  id={job.id} provider_id={job.provider_id} "
                f"is_active={job.is_active} slug={job.slug!r} title={job.title!r}"
            )


if __name__ == "__main__":
    main()
