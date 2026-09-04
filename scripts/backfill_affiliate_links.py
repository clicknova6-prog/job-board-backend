"""Backfill affiliate links for every active provider.

Usage from the repository root:
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
