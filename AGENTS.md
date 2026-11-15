# AGENTS.md

## Project

A **multi-site job hub**: one backend ingests every job feed (currently Jobg8, an hourly XML snapshot of ~350k jobs) into one PostgreSQL database and serves several niche job board sites from it. Jobs are assigned to sites by admin-editable routing rules. Build order: **IT & Tech first, Healthcare & Medical second**, further sites later. Each site's frontend (built by the frontend partner, same GitHub account) consumes the site-scoped public API.

Status: multi-site work on branch `feat/multi-site` (Nov 2026). Never deployed yet; next step is Oracle Cloud Free (ARM, Docker) with daily backups. Nothing should depend on a developer laptop.

## Locked Technology Decisions (DO NOT CHANGE WITHOUT EXPLICIT APPROVAL)

- FastAPI + SQLAlchemy 2.0 async for the API layer; sync SQLAlchemy for the import/ingestion layer (streaming XML must stay synchronous)
- PostgreSQL + hybrid relational/JSONB (`source_payload` preserves the raw feed record verbatim, never normalized values)
- Celery + Redis for background jobs; Celery Beat is DB-driven (reads `Provider.schedule_interval_minutes`), not a static schedule
- Alembic for all schema migrations — no manual schema changes ever
- Repository pattern: ORM objects touched only inside `app/db/repositories.py` (sync: import pipeline, routing, sitemaps) or `app/db/*_repositories.py` (async, API-facing) — never directly from service/task/router code
- Thin Celery tasks; real logic lives in service classes (`ImportService`, `PromotionService`, `SiteRoutingService`, ...)
- Public API field names are decoupled from internal DB column names via Pydantic serialization aliases (e.g. `advertiser_name`→`company`, `classification`→`category`, `apply_url`→`job_url`)
- **One hub, site-scoped API** (not one DB per site). Sites identified by the `X-Site-Key` header

## Multi-Site Architecture

- Tables: `sites` (slug, name, `site_key`, `base_url`, `is_active`, `config` JSONB), `site_routing_rules`, `job_sites` (many-to-many, records `matched_rule_id`)
- **Routing rules** (per site, optional `provider_id`), evaluated per job: any matching `exclude_title` removes the job; else first matching `include_classification` (exact normalized provider classification) or `include_title` (whole-word phrase) in rule-id order assigns it. Pure matcher: `app/sites/matcher.py`
- `SiteRoutingService` recomputes desired assignments per provider and applies only the diff. Runs after every completed import (failure never fails the import) and on demand (`POST /admin/api/sites/routing/run` → Celery `route_all_sites`). Inactive jobs keep their assignments during retention so detail pages stay scoped. Deactivated sites are left untouched
- Whether a job may appear on several sites is decided by real data later; the schema already allows it
- **Public API** (`/api/v1/jobs`, `/jobs/filters`, `/jobs/{slug}`) requires `X-Site-Key` (`app/api/site_dependencies.py`): missing → 400, unknown/inactive → 404, browser `Origin` not belonging to the site (its `base_url` + `config.cors_origins`) nor hub-wide (`CORS_ALLOWED_ORIGINS`) → 403. All reads filter by `job_sites`; another site's job is 404
- **CORS**: `SiteAwareCORSMiddleware` allows env origins plus origins of active sites (60s TTL registry, keeps last known set on DB errors; invalidated on admin site edits)
- **Filters cache**: key `filters:v{version}:site:{id}`; one version bump (after promotion and after routing) invalidates every site
- **SEO** per site from `sites.base_url`: sitemaps generated into `SITEMAP_OUTPUT_DIR/<slug>/`, served at `/sites/{slug}/robots.txt`, `/sites/{slug}/sitemap.xml`, `/sites/{slug}/sitemap-{n}.xml.gz` (each frontend rewrites its root paths to these). Job detail returns `canonical_url` = `{base_url}/job/{slug}`; JSON-LD `url` is that page
- **Apply** links go straight to the provider: `job_url` = `jobs.apply_url` (per-site click tracking deferred)
- **Admin**: `/admin/api/sites` CRUD, `/{id}/rules` CRUD, `/{id}/routing/preview` (dry run of saved or draft rules, writes nothing), `/routing/run`. All mutations audited in `admin_audit_logs`
- Seed/tooling: `config/sites/it-tech.json` + `python -m scripts.seed_sites` (idempotent), `python -m scripts.preview_site_routing <slug> [--csv ...]`

## Architecture Principles

- `provider_id` + `source_job_id` is the job identity (never `source_job_id` alone)
- Staging → validate → anomaly-check → promote → route to sites: the importer never writes directly to `jobs`
- Non-essential field parse failures fall back to `None` and are logged in `import_runs.field_fallback_warnings`; only `sender_reference`, `title`, `description`, `apply_url` are reject-worthy
- Unknown feed fields are captured via `extra="allow"` and logged in `import_runs.unmapped_fields`
- `payload_hash` is computed over the raw `source_record`, not normalized values
- Anomaly thresholds and soft-delete retention are per-provider in `Provider.config` JSONB, never hardcoded
- Public list endpoints paginate by KEYSET only, never OFFSET; tiebreaker direction matches the primary sort. Here `sort="-last_imported_at"` means ascending
- `/jobs/filters` is registered before `/jobs/{slug}`
- The affiliate/promo redirect system was REMOVED in Nov 2026 — traffic comes only from our own sites. Do not reintroduce without explicit approval
- Candidate auth is Google OAuth only; admins have separate email/password auth

## Deferred (not in progress)

- Per-site Apply click tracking; search/listing result caching (only filter metadata is cached)
- Site-scoping of saved jobs (`/api/v1/me`) — saved jobs are hub-wide for now
- Cross-site canonical choice for jobs on several sites (each site is currently self-canonical)
- Deleting sites via API (deactivate instead); XML export feature

## Development Environment

- Cloud-first: work in a cloud clone; local PostgreSQL + Redis for tests. On Windows (legacy): PostgreSQL service, Memurai, `.venv`, Celery `--pool=solo`
- Config via `python-dotenv` from `.env` (utf-8-sig); see `.env.example`
- Tests: `uv run python -m pytest -q` (DB tests create/migrate `job_board_test` from `DATABASE_URL`)
- Scripts run as `python -m scripts.<name>`
- `openapi.json` is regenerated with `json.dumps(app.openapi())` whenever the API changes

## Working Rules

- Audit before building; keep this file current as authoritative state
- Do not silently change requirements — conflicts with locked decisions: say "Potential issue / DECISION REQUIRED" and stop
- One concern per commit (`feat:` / `fix:` / `chore:` / `test:` / `docs:`); verify pushes (`git log origin/<branch>..HEAD` blank)
- All schema changes through Alembic migrations, reviewed before applying
- Prefer small, scoped diffs; don't reformat unrelated code (several older files are not ruff-formatted)
- Live verification (HTTP smoke test + DB state check) before any commit
- For every backend change, check whether the frontend team needs to know and draft the note
