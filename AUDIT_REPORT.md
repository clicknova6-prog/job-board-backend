# Production Readiness Audit — job-board-backend

Read-only audit performed 2026-09-04. Covers security, code quality, architecture consistency, database, tests, and dependencies. No files were modified as part of this review.

**Overall assessment:** this codebase is materially more mature and disciplined than `AGENTS.md`'s "Current Status" section suggests — auth, admin routes, affiliate links, Celery orchestration, and a 300+ function test suite are all built and largely well-tested. No Critical findings. The most consequential issues are a database indexing gap that undermines the project's own keyset-pagination guarantee under filters, and a couple of oversized/under-tested orchestration functions.

---

## Critical

None found.

---

## High

### H1. Filtered public job listings lose the keyset-pagination O(1)-per-page guarantee
**File:** [app/db/public_job_repositories.py:261-269](app/db/public_job_repositories.py#L261-L269) (filter predicates) vs. [app/db/models.py:328-347](app/db/models.py#L328-L347) (indexes)

`classification` and `country_name` each have their own separate single-column partial index, distinct from `jobs_active_last_imported_at_id_idx`. Postgres cannot use one index for an equality filter and another for the sort/keyset-comparison in a single scan — it will pick one and add an explicit `Sort`, demoting the keyset row-comparison predicate to a `Filter`. This is exactly the O(n²) deep-pagination problem the locked architecture rule and the code comment at [public_job_repositories.py:226-233](app/db/public_job_repositories.py#L226-L233) were written to prevent — it just silently stops applying the moment any filter is combined with pagination.

**Why it matters:** any `classification`/`employment_type`/`country_name`/`location` filter combined with deep pagination reintroduces full-scan-and-discard behavior on a catalogue already at ~335k rows (per the search-vector migration's own sizing comment).

**Fix:** add composite partial indexes `(is_active, classification, last_imported_at DESC, id DESC)` (and similarly for `country_name`) for the commonly-filtered columns, or explicitly document that filtered listings are not O(1) per page.

### H2. `employment_type` and `location` have no supporting index at all
**File:** [app/db/models.py:376-384](app/db/models.py#L376-L384)

Both columns are used as equality filters in `_apply_filters` ([app/db/public_job_repositories.py:262-266](app/db/public_job_repositories.py#L262-L266)), and `employment_type` is also grouped/counted in `get_filter_metadata` ([public_job_repositories.py:123-142](app/db/public_job_repositories.py#L123-L142)). Unlike their sibling filter columns (`classification`, `country_name`), neither has any index — confirmed across all 16 migrations.

**Why it matters:** filtering or grouping on `employment_type` forces a sequential/full-index scan with a Filter node on a ~335k-row table.

**Fix:** add `Index("jobs_active_employment_type_idx", "employment_type", postgresql_where=text("is_active = true"))`, matching the existing `classification`/`country_name` pattern. Confirm `location` is actually used as an exact-match filter before indexing it the same way (its own column comment notes it may be free-form, e.g. "Remote").

---

## Medium

### M1. Refresh-token reuse (theft) is not treated as a token-family compromise signal
**File:** [app/services/auth/jwt_service.py:154-156](app/services/auth/jwt_service.py#L154-L156) (`rotate_refresh_token`), [app/db/auth_repositories.py:213-222](app/db/auth_repositories.py#L213-L222) (`revoke_refresh_token`)

When an already-revoked (previously-rotated/used) refresh token is replayed, the code raises `InvalidRefreshTokenError` but does not revoke the rest of that subject's active sessions. Standard rotation-based refresh flows treat replay of a spent token as a theft signal and kill the whole token family. The "disabled owner" path does correctly revoke-all — this is specifically the "spent token replayed" branch.

**Fix:** in `rotate_refresh_token`, when `stored.revoked_at is not None`, call `revoke_all_refresh_tokens` for that subject before raising, and log the event.

### M2. `PromotionService.run()` is the most complex function in the codebase
**File:** [app/imports/promotion.py:71-239](app/imports/promotion.py#L71-L239) (~170 lines)

Does anomaly checking, batched upsert, post-flush slug assignment, per-batch affiliate-link generation with its own try/except, stale-job deactivation, and a second affiliate-link backfill pass — largely sequential, but with two near-duplicate affiliate-link-generation blocks (lines ~163-177 and ~192-208).

**Fix:** split into `_run_anomaly_check`, `_promote_batches`, `_backfill_missing_affiliate_links` private methods; unify the two affiliate-link try/except blocks into one helper.

### M3. `run_provider_import` Celery task carries orchestration/error-classification logic, not just a service call
**File:** [app/tasks/import_tasks.py:43-154](app/tasks/import_tasks.py#L43-L154)

110+ lines including a `_log_context` helper, nested try/except/finally, and run-status reconciliation (`session.rollback(); run = provider_repo.get_import_run(...)`) directly in the task body — more than the "thin Celery task" principle calls for, even though the real work is still delegated to `ImportService`/`PromotionService`/`DownloadService`.

**Fix:** extract the retry/error-classification branch into an `ImportOrchestrationService.run(provider_id)`, leaving the task as a one-line call.

### M4. No integration test runs the import→promotion pipeline against real Postgres end-to-end
**File:** [tests/test_import_tasks.py](tests/test_import_tasks.py) (mocks every collaborator), [tests/test_importer.py](tests/test_importer.py) / [tests/test_promotion.py](tests/test_promotion.py) (unit tests against stub repositories)

The only place the full staged→promoted flow runs against a real database is the manual `scripts/test_import_cycle.py`. Repository-layer SQL (batch upsert, `deactivate_stale_jobs`, unique-constraint interplay) is therefore only verified against fakes, never real Postgres semantics.

**Fix:** add one integration test using the same `test_database_url` fixture pattern already used by `tests/api/v1/`.

### M5. No rate limiting (or test for one) on the admin manual-import-trigger endpoint
**File:** [app/api/routers/admin_imports.py](app/api/routers/admin_imports.py) (`POST /admin/api/imports/providers/{id}/trigger`), [tests/test_admin_management_routes.py](tests/test_admin_management_routes.py)

Public auth routes explicitly assert 429 rate-limit behavior; this endpoint — which enqueues a real Celery import job — has no equivalent test, and it's unclear from the router whether a limit is applied at all. A compromised or careless admin token could trigger unbounded import runs.

**Fix:** apply/verify a rate limit on this route and add a test mirroring `test_public_refresh_logout_and_refresh_rate_limit`.

### M6. `RefreshToken.token_hash` index is not unique
**File:** [app/db/models.py:659](app/db/models.py#L659), migration `ae7ed75aa030:147-152`

Nothing in the schema prevents two rows sharing a `token_hash`. `get_refresh_token_for_update` ([app/db/auth_repositories.py:165-186](app/db/auth_repositories.py#L165-L186)) uses `.one_or_none()` so a collision would raise rather than silently authenticate the wrong session (fails safe today), but the guarantee should come from the database, not from hash-collision improbability alone.

**Fix:** add a unique index/constraint on `token_hash`.

### M7. `redis` is imported directly but not declared as a direct dependency
**File:** [app/core/rate_limit.py](app/core/rate_limit.py), [app/core/filters_cache.py](app/core/filters_cache.py) (`import redis` / `redis.asyncio`), [pyproject.toml](pyproject.toml)

`redis` is only present transitively via `celery[redis]`/`slowapi`. If either drops or re-pins it, app code importing it directly would break with no signal from `pyproject.toml`.

**Fix:** add `redis` as an explicit direct dependency pinned at its currently-resolved floor.

### M8. Business-eligibility logic embedded in an admin router instead of the service layer
**File:** [app/api/routers/admin_affiliate.py:63-101](app/api/routers/admin_affiliate.py#L63-L101) (`generate_affiliate_links`)

The loop classifying jobs into `valid_job_ids`/`excluded` is policy (which jobs are eligible for affiliate-link generation), not request wiring, and belongs in `AffiliateService` per the project's service-layer-boundary rule.

**Fix:** move the classification loop into `AffiliateService` (e.g. `revalidate_and_generate(...)`), leaving the router as a thin translator.

---

## Low

### L1. No PKCE on Google OAuth flow
**File:** [app/auth/router.py:60-121](app/auth/router.py#L60-L121)

State-cookie CSRF protection (random 32-byte state, `secrets.compare_digest`, httponly/secure cookie scoped to `/auth/google`) is implemented correctly, but there's no PKCE (`code_verifier`/`code_challenge`). Defense-in-depth gap rather than an exploitable flaw given Google's own protections.

**Fix:** add PKCE to the authorization/token exchange.

### L2. CORS config doesn't guard against `*` + credentials misconfiguration
**File:** [app/main.py:29-35](app/main.py#L29-L35)

Current default is a specific localhost origin (safe), but nothing stops an operator from setting `CORS_ALLOWED_ORIGINS=*` in `.env` alongside `allow_credentials=True` — browsers reject this combination, but it's worth failing fast in config validation rather than relying on client-side rejection.

**Fix:** validate in `app/core/config.py` that `*` is never combined with `allow_credentials=True`.

### L3. Auth events (login success/failure, logout) are not audit-logged
**File:** [app/auth/admin_router.py](app/auth/admin_router.py)

Provider mutations and import triggers call `record_admin_action`, but authentication events themselves don't, reducing forensic visibility if an admin credential is compromised.

**Fix:** add audit-log entries for admin login/logout/failed-login events.

### L4. JWT algorithm has no explicit allowlist assertion
**File:** [app/core/auth_config.py:29-35](app/core/auth_config.py#L29-L35), [app/services/auth/jwt_service.py:69-73,101-108](app/services/auth/jwt_service.py#L69-L108)

`JWT_ALGORITHM` is read from env and used directly for both encode and decode (same secret both directions, so no classic alg-confusion risk today), but nothing explicitly restricts it to `HS256/384/512`.

**Fix:** assert the configured algorithm is in an HMAC allowlist at startup.

### L5. `AdminUser`/import-run history admin views use OFFSET pagination
**File:** [app/db/import_repositories.py:71-90](app/db/import_repositories.py#L71-L90) (`list_import_runs`)

Not a violation of the public-endpoint keyset rule (this is admin-only), but `import_runs` grows roughly hourly forever (see N1 below), so deep-offset pages will get progressively more expensive.

**Fix:** revisit if this view becomes long-lived/high-volume; not urgent now.

### L6. Sitemap keyset query has no dedicated composite index
**File:** [app/db/repositories.py:804-819](app/db/repositories.py#L804-L819) (`list_active_jobs_after`)

`WHERE is_active = true AND id > :after_id ORDER BY id LIMIT :n` has no `(is_active, id)` index; Postgres likely scans the PK index and filters `is_active` as a row Filter, which is cheap today but may degrade as soft-deleted rows accumulate under the retention window.

**Fix:** add a dedicated `(is_active, id)` partial index only if sitemap generation is later profiled as degrading.

### L7. Duplicated small helpers across routers
**Files:** `app/auth/router.py` and `app/auth/admin_router.py` each define their own `_access_token_response`; `_provider_not_found()` is duplicated identically in [app/api/routers/admin_imports.py:157-162](app/api/routers/admin_imports.py#L157-L162) and [admin_providers.py:142-147](app/api/routers/admin_providers.py#L142-L147), with similar one-off `_import_run_not_found`/`_job_not_found`/`_user_not_found` functions.

**Fix:** consolidate into shared `app/auth/responses.py` and `app/api/not_found.py` helpers. Low risk today, but drift-prone.

### L8. Two different error-body shapes across the API surface
**Files:** `app/auth/router.py`/`admin_router.py` use an `error_content()`/`error_code()` envelope (`{"code":..., "message":...}`); `app/api/v1/jobs.py`, `me.py`, `admin_imports.py`, `admin_providers.py` raise bare `HTTPException(detail=...)` (FastAPI's default `{"detail":...}`).

**Fix:** standardize on one error envelope if API consumers are expected to parse error shapes generically.

### L9. `Job.last_seen_import_run_id` FK is `ON DELETE RESTRICT` with no `import_runs` retention policy
**File:** [app/db/models.py:420-423](app/db/models.py#L420-L423)

Looks intentional (audit trail), but `import_runs` grows without an explicit archival/retention policy the way `jobs` has one — flagging as a decision point, not a bug, since `AGENTS.md` doesn't specify one for this table.

### L10. Affiliate redirect on inactive jobs isn't documented as intentional
**File:** [app/api/routers/redirect.py:26-31](app/api/routers/redirect.py#L26-L31), test at `tests/test_affiliate_routes.py:228`

Soft-deleted (`is_active=False`) jobs still redirect to their live `apply_url` rather than an "unavailable" page — only a missing short-hash gets the unavailable redirect. Likely correct (a shared link should keep working during the retention window), but neither the code nor the test explains why.

**Fix:** add a one-line comment/docstring note or a more explicit test name capturing the intent.

---

## Nitpick

### N1. Dead SSRF-adjacent helper function
**File:** [app/imports/downloader.py:103-105](app/imports/downloader.py#L103-L105) (`_validate_public_http_url`)

Zero call sites anywhere in `app/` (confirmed via search) — the actual SSRF guard is `_resolve_public_http_url`, used from `_target_for_request` and `_ValidatedRedirectHandler.redirect_request`. Not harmful, but a trap: a future reader could reasonably assume the unused wrapper is the guarding function.

**Fix:** remove it, or confirm whether a pre-check call site was intended and got dropped.

### N2. `AffiliateLink.provider_id` has no dedicated index
**File:** [app/db/models.py:474-478](app/db/models.py#L474-L478)

Only ever queried in combination with `Job.provider_id`, so current impact is negligible; flagging only because every other FK on this table is index-reachable and this one isn't.

### N3. `AdminAuditLog` lacks a composite index for the likely common query shape
**File:** [app/db/models.py:623-629](app/db/models.py#L623-L629)

Separate indexes on `admin_user_id`, `(target_type, target_id)`, and `created_at` — a "recent actions for admin X" query filtering `admin_user_id` and ordering by `created_at DESC` hits the same single-vs-composite limitation as H1, at much lower row volume (low practical impact).

### N4. `limits` package used directly in tests but not declared
**File:** [tests/](tests/) (imports `limits.storage.MemoryStorage`, `limits.strategies.FixedWindowRateLimiter`), pulled in transitively via `slowapi`.

Lower risk than M7 since only test code imports it directly, not app code. Consider declaring as a dev dependency for the same reason as M7.

### N5. `pyproject.toml` uses lower-bound-only version specifiers
**File:** [pyproject.toml](pyproject.toml)

All dependencies use `>=` with no upper bound; reproducibility relies entirely on the committed `uv.lock` (currently pinned to current, non-vulnerable versions — alembic 1.18.5, fastapi 0.139.0, sqlalchemy 2.0.51, pyjwt 2.13.0, etc.). Safe as long as installs always use the lockfile.

**Fix:** add a one-line note in `AGENTS.md` that `uv.lock` is authoritative for versions.

### N6. `AGENTS.md` "Current Status" section is stale
The doc describes auth, admin routes, affiliate handling, and Celery task scheduling as unbuilt or partial; all are implemented and tested (303 test functions across 25 modules). Not a code defect, but worth updating so the doc stays a reliable map of the codebase.

---

## What's done well (for context, not action items)

- **SSRF protection on feed downloads** ([app/imports/downloader.py](app/imports/downloader.py)) is unusually thorough: scheme/credential/localhost rejection, DNS-pinned connections with `is_global` validation, per-redirect-hop re-validation, disabled system proxies, and download/extraction size caps — with matching negative-path test coverage.
- **Every admin route is gated** via router-level `Depends(require_admin_role(...))`; no route can be added without auth by accident.
- **No SQL injection surface anywhere** — every repository uses the ORM/Core query builder or bound params exclusively; `websearch_to_tsquery` is used (not `to_tsquery`) so malformed search input can't 500.
- **Auth fundamentals are solid**: short-lived HS256 access tokens, high-entropy opaque refresh tokens stored only as SHA-256 hashes, atomic rotation under `SELECT ... FOR UPDATE`, Argon2id password hashing, correctly-flagged cookies, timing-safe OAuth state comparison, uniform login-failure messaging.
- **Rate limiting has good breadth**: login, OAuth, refresh, admin API, public search, and redirect endpoints are all covered with sensible per-route limits.
- **Strict repository-pattern and provider-agnostic-service discipline**: no router or task touches ORM objects directly; no Jobg8-specific assumption was found leaking outside `app/imports/`.
- **The keyset-pagination design for the unfiltered default listing is implemented exactly as specified** — the DESC/DESC index, the row-comparison predicate, and dedicated tests for tie-breaking and cursor edge cases all line up.
- **Migrations and models are in sync** — all 16 migrations were walked in order with zero drift against `app/db/models.py`.
- **Test suite is unusually mature**: deep, deterministic coverage of the import/promotion pipeline (including exact anomaly-threshold boundaries and retention-hour cutoffs), no flaky patterns found (no sleep-based timing, no unmocked network calls, no expiring hardcoded dates), and assertions check full response bodies rather than just status codes.
- **No missing type hints** found across repositories or services — annotations are consistently applied, including private helpers.
