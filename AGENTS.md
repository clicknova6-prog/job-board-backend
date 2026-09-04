# AGENTS.md

## Project

This is a job board backend built around multi-provider job feed ingestion — currently a single provider, Jobg8, delivering an hourly XML snapshot (~350k jobs). PostgreSQL is the source of truth. The backend is **feature-complete, audited, and handed off to the frontend team.** A frontend partner (same GitHub account) runs it locally and consumes the public API. Work from here is frontend-integration support: bug reports, contract questions, and occasional backend fixes only when a concrete gap is found — not new feature development by default.

## Locked Technology Decisions (DO NOT CHANGE WITHOUT EXPLICIT APPROVAL)

- FastAPI + SQLAlchemy 2.0 async for the API layer; sync SQLAlchemy for the import/ingestion layer (streaming XML must stay synchronous)
- PostgreSQL + hybrid relational/JSONB (`source_payload` preserves the raw feed record verbatim, never normalized values)
- Celery + Redis (Memurai on Windows, no Docker/WSL) for background jobs; Celery Beat is DB-driven (reads `Provider.schedule_interval_minutes`), not a static schedule
- Alembic for all schema migrations — no manual schema changes ever
- Repository pattern: ORM objects touched only inside `app/db/repositories.py` (sync, import pipeline) or `app/db/*_repositories.py` (async, API-facing) — never directly from service/task/router code
- Thin Celery tasks; real logic lives in service classes (`ImportService`, `PromotionService`, `AffiliateService`, etc.)
- Public API field names are decoupled from internal DB column names via Pydantic serialization aliases (e.g. `advertiser_name`→`company`, `classification`→`category`, `apply_url`→`job_url`) — insulates the frontend from internal refactors

## Architecture Principles

- `provider_id` + `source_job_id` is the job identity (never `source_job_id` alone — only unique within a provider)
- Staging → validate → anomaly-check → promote: the importer never writes directly to `jobs`
- Non-essential field parse failures fall back to `None` and are logged in `import_runs.field_fallback_warnings`, never reject the record. Only `sender_reference`, `title`, `description`, `apply_url` are essential/reject-worthy
- Unknown feed fields are captured via `extra="allow"` and logged in `import_runs.unmapped_fields`, never rejected
- `payload_hash` is computed over the raw `source_record`, not normalized values
- Anomaly thresholds and soft-delete retention window are configurable per-provider via `Provider.config` JSONB, never hardcoded
- Public list endpoints paginate by KEYSET only, never OFFSET. Tiebreaker column direction must match the primary sort direction
- `jobs.apply_url` is the single field feeding both the public Apply link and the `/r/{short_hash}` affiliate redirect
- Affiliate `redirect_url` links are auto-generated in bulk during promotion (`app/imports/promotion.py`) — not a manual-only admin action
- Candidate/job-seeker auth is Google OAuth only — no email/password for candidates (admin retains separate full email/password auth)

## Current Status — BACKEND COMPLETE

All spec sections (public API, admin panel §19, affiliate system, SEO/sitemap, auth, rate limiting, caching) are built, tested, and merged to `main`. A full read-only production audit (Sept 2026) found 0 Critical / 0 unresolved High issues; findings fixed are documented in `AUDIT_REPORT.md` at repo root. Full test suite: 456 passing, 100% coverage on `app/tasks/*.py` and `app/celery_app.py`.

**Built (all of it):**
- Full ORM schema, importer/promotion pipeline, streaming XML parsing, staging, anomaly detection
- `app/api/v1/jobs.py` — public search/listing + job detail (`GET /api/v1/jobs/{slug}`), keyset pagination, full-text search via `search_vector`
- `app/api/routers/redirect.py` — `/r/{short_hash}` affiliate redirect, including post-hard-delete fallback page
- Admin panel — provider management, import history/health, rejected records, anomaly warnings, manual import trigger, affiliate link generator, admin audit logging (`admin_imports.py`, `admin_providers.py`, `admin_affiliate.py`)
- Auth — JWT access/refresh for public users + admins, Google OAuth, refresh-token-family revocation on reuse detection
- SEO — sitemap generation, canonical URLs, structured data support
- Celery Beat DB-driven scheduling, hard-delete cleanup worker (12hr+ configurable retention)
- Rate limiting (auth + admin manual-import-trigger endpoints), Redis-cached filter metadata (version-invalidated)
- `scripts/seed_dev_data.py` + `SETUP.md` — full local Windows setup guide for the frontend partner
- `scripts/backfill_affiliate_links.py` — one-time backfill, already run against full dataset (349,950 links, idempotent-verified)

**Explicitly deferred, not in progress:**
- Search/listing result caching (filter metadata is cached; result caching deferred until real production traffic exists to measure against)
- Staging/VPS deployment (frontend partner runs backend locally via GitHub access; Hostinger VPS is the pick if revisited)
- XML export (outgoing) feature — deprioritized indefinitely, no confirmed use case
- Low/Nitpick audit findings (PKCE, CORS validation hardening, error-shape edge-case consistency) — reviewed, deliberately skipped as optional polish

## Windows Development Environment

- No Docker, no WSL. Local PostgreSQL 18 service, Memurai for Redis on port 6379
- Python venv at `.venv`, activate before running anything
- Config loaded via `python-dotenv` from `.env` (utf-8-sig encoding, has a BOM)
- Celery on Windows requires `--pool=solo`
- Scripts in `scripts/` run as `python -m scripts.scriptname`, not directly, unless they have the sys.path bootstrap

## Working Rules

- This is a **maintenance/integration phase**, not greenfield development. Don't propose new features unless the frontend partner surfaces a concrete gap.
- Do not silently change requirements — if something conflicts with locked decisions above, say "Potential issue / DECISION REQUIRED" and stop, don't guess
- Do not touch files outside what's explicitly asked in a given task
- All schema changes go through Alembic migrations, reviewed before applying
- Prefer small, scoped diffs over large rewrites
- Live verification (curl smoke test + DB state check) required before any commit