# AGENTS.md

## Project

This is a job board backend built around multi-provider job feed ingestion — currently a single provider, Jobg8, delivering an hourly XML snapshot. PostgreSQL is the source of truth for both the raw staged feed data and the canonical, publicly-servable job catalogue. The FastAPI layer is now partly real: `GET /api/v1/jobs` (public search/listing) serves live rows from the database, and the old placeholder `app/jobs/` stub has been removed. The remaining public routes (job detail, affiliate redirect) and the whole admin surface are still unbuilt. Celery is wired up for background work (broker/backend configured, a health-check task exists) but no import/promotion scheduling task exists yet.

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
- Public list endpoints paginate by KEYSET, never OFFSET. The `id` tiebreaker must follow the primary sort direction so the cursor predicate stays a row comparison (`(last_imported_at, id) < (:v, :id)` for DESC, `>` for ASC), and `jobs_active_last_imported_at_id_idx` must keep BOTH key columns DESC so one index serves both directions. Forcing `id ASC` under a DESC primary sort demotes the predicate to a Filter and makes deep pages O(n^2) — a feed import stamps every row with the same `last_imported_at`, so ties are the norm, not the exception
- The public job destination uses a single `apply_url` field per job; Jobg8 maps its `<URL>` tag to `apply_url`

## Current Status

**Built:**
- `app/db/models.py` — full SQLAlchemy 2.0 ORM schema: `Provider`, `ImportRun`, `JobStaging`, `Job`. Includes check constraints, indexes, and the `jobs_provider_source_job_unique` / `jobs_source_identity_unique` constraints.
- `app/imports/schemas.py` — `JobFeedRecord` Pydantic model mapping the Jobg8 `<Job>` XML element. Handles blank-string-as-missing normalization, currency code extraction, salary/sell-price coercion with fallback, lenient apply_url acceptance, inverted salary range correction, and `extra="allow"` capture of unmapped fields via `model_extra`.
- `app/imports/hashing.py` — `compute_payload_hash()`, SHA-256 over the canonicalized raw `source_record` (not normalized values), matching the locked payload_hash decision.
- `app/imports/importer.py` — `ImportService`: streams a feed file through the parser, stages valid records via `JobRepository`, tracks `unmapped_fields` / `field_fallback_warnings` counts, commits once per full run.
- `app/imports/promotion.py` — `PromotionService`: runs the anomaly check (feed-drop % and rejection-rate % against `Provider.config`), upserts staged rows into `jobs` in batches, soft-deletes (`is_active=False`, `deactivated_at`) jobs the run didn't see. Aborting on anomaly leaves `jobs` completely untouched.
- `app/db/repositories.py` — `JobRepository` and `PromotionRepository`, the only code that touches ORM objects for the import pipeline.
- `app/db/session.py` — sync engine/session setup, loads `.env` via `python-dotenv` (utf-8-sig).
- `app/celery_app.py` + `app/tasks/health.py` — Celery app configured with Redis broker/backend; includes a `ping` health-check task.
- `app/services/auth/` (`jwt_service.py`, `admin_auth_service.py`, `google_oauth_service.py`) + `app/auth/` (`router.py`, `admin_router.py`, `dependencies.py`, `cookies.py`) — JWT access/refresh auth for public users and admins, Google OAuth login, role-gated admin dependency. DONE, tested in `tests/test_jwt_service.py`, `test_admin_auth_service.py`, `test_google_oauth_service.py`, `test_auth_routes.py`.
- `app/api/routers/admin_imports.py`, `admin_providers.py`, `admin_affiliate.py` — admin API routes for triggering/inspecting imports, managing providers, and generating affiliate links; every route is gated behind `require_admin_role`. DONE, tested in `tests/test_admin_management_routes.py`.
- `app/services/affiliate_service.py` + `app/api/routers/redirect.py` — affiliate link generation and the public redirect endpoint. DONE, tested in `tests/test_affiliate.py`, `tests/test_affiliate_routes.py`.
- `app/tasks/scheduler_tasks.py` + `app/tasks/import_tasks.py` — Celery Beat schedule reads `Provider.schedule_interval_minutes` and dispatches `run_provider_import` per provider, with retry handling for transient errors. DONE, tested in `tests/test_scheduler_tasks.py`, `test_import_tasks.py`.
- `app/main.py` — FastAPI app. Mounts the auth routers and the v1 public API at `/api/v1`. `GET /` is a liveness message; every job route now reads real `Job` rows.
- `app/api/v1/jobs.py` — **`GET /api/v1/jobs`, the public job search/listing endpoint (DONE).** Filters on `classification`, `employment_type`, `country_name`, `location`, and `q` (full-text); always constrains `is_active = true`; sorts by `last_imported_at` (`-last_imported_at` selects ASCENDING, inverting the usual `-` convention). Keyset pagination only — never OFFSET.
- `app/api/v1/cursors.py` — opaque base64 `{"v": <last_imported_at>, "id": <id>}` page tokens. A malformed cursor is a 400, never a 500.
- `app/schemas/job_public.py` — `JobSummary` / `JobListResponse`. This is the public contract: never add `source_payload`, `payload_hash`, `provider_id`, `source_job_id`, or `last_seen_import_run_id` to it.
- `app/db/public_job_repositories.py` — async read-side repository for the public API, following the `app/db/auth_repositories.py` split (ORM objects stay inside; callers get frozen dataclasses). `app/db/repositories.py` remains the SYNC import-pipeline repository.
- `app/db/async_session.py` — async engine (`postgresql+asyncpg`) plus the `get_async_session` FastAPI dependency, shared by the auth and public API routes.
- `jobs.search_vector` — PostgreSQL `GENERATED ALWAYS ... STORED` tsvector over title+description (weighted A/B), with a GIN index. The `english` regconfig in `SEARCH_TEXT_CONFIG` MUST match the one baked into the column or `q` silently returns nothing.
- `tests/api/v1/` — pytest integration tests for the public API. They create and migrate a separate `job_board_test` database (override with `TEST_DATABASE_URL`); the dev database is never touched.
- Alembic — initialized, 9 migrations applied so far. The latest, `b3c91f4d27ae`, adds `jobs.search_vector` (generated tsvector + GIN) and `jobs_active_last_imported_at_id_idx`. Note it rewrites the `jobs` table, so it takes minutes on a full catalogue.
- `scripts/` — `seed_provider.py`, `audit_full_feed.py`, `extract_sample.py`, `test_import_cycle.py` (the manual end-to-end verification workflow, since there's no pytest suite yet).

**NOT built yet:**
- **API route structure — IN PROGRESS.** `GET /api/v1/jobs` (public search/listing) is DONE. Still missing: job detail (`GET /api/v1/jobs/{slug}`), and any filter-facet or aggregate endpoints
- SEO / sitemap generation
- Hard-delete cleanup worker (soft-delete + retention window exist in principle; nothing purges rows past the retention period)

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