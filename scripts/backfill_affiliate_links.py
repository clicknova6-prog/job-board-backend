"""
One-time backfill: generate affiliate links for active jobs that don't have one.

WHY THIS EXISTS:
Affiliate links are normally auto-generated during every import (see
PromotionService in app/imports/promotion.py). This script only exists because
~350k jobs went live BEFORE that automatic generation was added, so they never
got links. This script closes that one-time gap.

DO YOU NEED TO RUN THIS AGAIN?
Not for normal operation — new jobs get links automatically on every import,
with no cap. Re-run this script only if:

- A new provider is added and its historical jobs need a one-time catch-up
  (same situation as this backfill).
- You suspect a bug or outage caused some active jobs to be missing links
  (check with the SQL query below first).
- You want a quick way to verify/enforce "zero active jobs missing links"
  at any point.

Safe to re-run any time - it's idempotent (skips jobs that already have a link).

To check if this is even needed before running:
SELECT COUNT(*) FROM jobs j LEFT JOIN affiliate_links a ON a.job_id = j.id
WHERE j.is_active = true AND a.id IS NULL;
(0 = nothing to do)

Usage:
python scripts/backfill_affiliate_links.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from time import perf_counter

# scripts/ is not a package, so direct script execution needs the repository
# root on sys.path before importing application modules.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.db.repositories import PromotionRepository, SchedulerRepository
from app.db.session import SessionLocal

BATCH_SIZE = 5000

logger = logging.getLogger(__name__)


def main() -> None:
    """Create all missing active-job affiliate links in bounded batches."""
    started_at = perf_counter()
    created_by_provider: dict[str, int] = {}

    with SessionLocal() as session:
        providers = SchedulerRepository(session).list_active_provider_schedules()
        repository = PromotionRepository(session)

        for provider in providers:
            provider_total = 0

            while True:
                job_ids = repository.find_active_jobs_missing_affiliate_link(
                    provider.provider_id,
                    limit=BATCH_SIZE,
                )
                if not job_ids:
                    break

                repository.bulk_create_affiliate_links(
                    provider.provider_id,
                    job_ids,
                )
                repository.commit()
                provider_total += len(job_ids)
                logger.info(
                    "Affiliate backfill progress: provider=%s batch=%d total=%d",
                    provider.provider_name,
                    len(job_ids),
                    provider_total,
                )

            created_by_provider[provider.provider_name] = provider_total

    runtime_seconds = perf_counter() - started_at
    total_created = sum(created_by_provider.values())

    print("Affiliate link backfill complete")
    print(f"Total links created: {total_created}")
    print("Per provider:")
    for provider_name, created_count in created_by_provider.items():
        print(f"  {provider_name}: {created_count}")
    print(f"Runtime: {runtime_seconds:.2f} seconds")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    main()
