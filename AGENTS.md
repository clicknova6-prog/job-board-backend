# AGENTS.md

## Project

This is a job board backend built around multi-provider job feed ingestion — currently a single provider, Jobg8, delivering an hourly XML snapshot. PostgreSQL is the source of truth for both the raw staged feed data and the canonical, publicly-servable job catalogue. A FastAPI layer is scaffolded (app/main.py, app/jobs/) but not yet real — it currently returns placeholder responses, not live data from the database. Celery is wired up for background work (broker/backend configured, a health-check task exists) but no import/promotion scheduling task exists yet.

## Locked Technology Decisions (DO NOT CHANGE WITHOUT EXPLICIT APPROVAL)

- FastAPI + SQLAlchemy 2.0 async for API layer (SCAFFOLDED ONLY — routes are placeholder stubs, not wired to the database yet)
- Sync SQLAlchemy for import/ingestion layer (app/db/session.py) — intentional, not a bug, streaming XML processing must stay synchronous
- PostgreSQL + hybrid relational/JSONB (raw_payload/source_payload columns preserve original feed data verbatim, always — never store normalized values there)
- Celery + Redis (via Memurai on Windows, no Docker/WSL in this dev environment) for background jobs
- Alembic for all schema migrations — no manual schema changes ever
- Repository pattern: ORM objects are only touched inside app/db/repositories.py, never directly from service/importer/task code
- Thin Celery tasks, real logic lives in service classes (ImportService, PromotionService, etc.)

## Architecture Principles

- provider_id + source_job_id is the identity for a job (never source_job_id alone — it's only unique within a provider)
- Staging → validate → anomaly-check → promote pattern: never write directly to `jobs` table from the importer
- Non-essential field parse failures fall back to None + get logged in import_runs.field_fallback_warnings — they NEVER reject the whole record. Only sender_reference, title, description, apply_url are essential (reject-worthy) fields
- Unknown/new feed fields are captured via extra="allow" and logged in import_runs.unmapped_fields, never rejected
- payload_hash is computed over RAW provider data (source_record), not normalized values — this is intentional so source_payload and its hash never drift apart
- Anomaly thresholds (feed drop %, rejection rate %) are configurable per-provider via Provider.config JSONB, not hardcoded
- Deleted/removed jobs are soft-deleted (is_active=False, deactivated_at set) with a retention window before hard delete — retention period is ALSO configurable per-provider via Provider.config JSONB (not hardcoded 12 hours)
- PROVIDER NOTE (Jobg8): the feed's <URL> tag serves as BOTH apply_url and affiliate_url for this provider. Other providers may supply these separately — the schema's 3-field separation (source_job_url, apply_url, affiliate_url) must be preserved for that reason

## Current Status

**Built:**
- `app/db/models.py` — full SQLAlchemy 2.0 ORM schema: `Provider`, `ImportRun`, `JobStaging`, `Job`. Includes check constraints, indexes, and the `jobs_provider_source_job_unique` / `jobs_source_identity_unique` constraints.
- `app/imports/schemas.py` — `JobFeedRecord` Pydantic model mapping the Jobg8 `<Job>` XML element. Handles blank-string-as-missing normalization, currency code extraction, salary/sell-price coercion with fallback, lenient apply_url acceptance, inverted salary range correction, and `extra="allow"` capture of unmapped fields via `model_extra`.
- `app/imports/hashing.py` — `compute_payload_hash()`, SHA-256 over the canonicalized raw `source_record` (not normalized values), matching the locked payload_hash decision.
- `app/imports/importer.py` — `ImportService`: streams a feed file through the parser, stages valid records via `JobRepository`, tracks `unmapped_fields` / `field_fallback_warnings` counts, commits once per full run.
- `app/imports/promotion.py` — `PromotionService`: runs the anomaly check (feed-drop % and rejection-rate % against `Provider.config`), upserts staged rows into `jobs` in batches, soft-deletes (`is_active=False`, `deactivated_at`) jobs the run didn't see. Aborting on anomaly leaves `jobs` completely untouched.
- `app/db/repositories.py` — `JobRepository` and `PromotionRepository`, the only code that touches ORM objects for the import pipeline.
- `app/db/session.py` — sync engine/session setup, loads `.env` via `python-dotenv` (utf-8-sig).
- `app/celery_app.py` + `app/tasks/health.py` — Celery app configured with Redis broker/backend; only task so far is a `ping` health check. No import/promotion task exists yet.
- `app/main.py` + `app/jobs/` — FastAPI app with one router mounted at `/jobs`. `GET /jobs/` and `GET /` both return hardcoded placeholder JSON, not real data from `Job` rows.
- Alembic — initialized, 5 migrations applied so far (initial schema, provider model + job.provider_id, import_run counters, unmapped_fields, field_fallback_warnings).
- `scripts/` — `seed_provider.py`, `audit_full_feed.py`, `extract_sample.py`, `test_import_cycle.py` (the manual end-to-end verification workflow, since there's no pytest suite yet).

**NOT built yet:**
- Real FastAPI routes backed by the database (job listing, job detail, search/filter)
- Admin panel / provider management UI
- Celery task(s) that actually run `ImportService` + `PromotionService` on a schedule, and the schedule itself (`Provider.schedule_cron` exists as a column but nothing consumes it)
- Affiliate redirect handling
- SEO / sitemap generation
- Hard-delete cleanup worker (soft-delete + retention window exist in principle; nothing purges rows past the retention period)
- `source_job_url` / `affiliate_url` population — both columns exist on `Job`, but `JobFeedRecord` and `JobStaging` don't map them yet, so they're always `None` today
- Automated test suite (no pytest present; `tests/fixtures` exists but verification is currently manual via `scripts/test_import_cycle.py`)

## Windows Development Environment

- No Docker, no WSL. Local PostgreSQL 18 service (postgresql-x64-18), Memurai for Redis on port 6379
- Python venv at .venv, activate before running anything
- DATABASE_URL, REDIS_BROKER_URL, REDIS_RESULT_BACKEND_URL are loaded via python-dotenv from .env (utf-8-sig encoding, has a BOM) — see .env.example for the full list of expected vars
- Celery on Windows requires --pool=solo (default prefork pool doesn't work on Windows)
- Scripts in scripts/ must be run as `python -m scripts.scriptname`, not `python scripts/scriptname.py` directly, unless they have the sys.path bootstrap (check the individual script)

## Working Rules

- Do not silently change requirements — if something conflicts with locked decisions above, say "Potential issue / DECISION REQUIRED" and stop, don't guess
- Do not touch files outside what's explicitly asked in a given task
- All schema changes go through Alembic migrations, reviewed before applying
- Prefer small, scoped diffs over large rewrites
