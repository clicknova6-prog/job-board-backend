---
name: run-import-cycle
description: Use when manually verifying a change to the import/promotion pipeline (ImportService, PromotionService) against a real local Postgres database. There is no pytest suite yet — this is the actual test workflow for that code.
---

No pytest is configured (only `tests/fixtures/` exists). Pipeline changes are verified with the `scripts/` scripts against a real `DATABASE_URL`-configured Postgres:

1. Export `DATABASE_URL` yourself first. `scripts/test_import_cycle.py` reads it straight from `os.environ` and does **not** load `.env` — only the `app.db.session` import path does that.
2. `uv run python scripts/seed_provider.py` — idempotent, ensures the `"jobg8"` Provider row exists (required before any `ImportRun`).
3. `uv run python scripts/test_import_cycle.py` — runs one full stage-then-promote cycle against `tests/fixtures/sample_feed.xml` and prints counts.
4. To refresh that fixture from a real feed: `uv run python scripts/extract_sample.py <zip_path>`.
5. To check how a schema/validation change would affect the full ~350k-job feed before relying on it: `uv run python scripts/audit_full_feed.py <zip_path>` — read-only, no DB writes, reports per-field failure rates.

Any new one-off script needs the same `sys.path` bootstrap these use (`scripts/` isn't a package, so the repo root isn't importable by default).
