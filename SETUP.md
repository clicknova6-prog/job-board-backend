# Local Development Setup (Windows)

This guide gets the backend running locally on Windows with PowerShell. No
Docker and no WSL are used anywhere in this project.

You do **not** need the real Jobg8 feed or Jobg8 credentials to develop
against this locally. `scripts/seed_dev_data.py` (see step 7) generates a
realistic synthetic job catalogue directly in your database, shaped exactly
like real Jobg8-imported data, so the API and frontend have real-looking
data to work against without ever touching the live feed.

## 1. Clone the repo

```powershell
git clone <repo-url> job-board-backend
cd job-board-backend
```

## 2. Python environment (uv)

This project uses [uv](https://docs.astral.sh/uv/) for dependency management
and a `.venv` virtual environment.

```powershell
uv sync
```

This creates `.venv` and installs both the main and `dev` dependency groups
(FastAPI, SQLAlchemy, Alembic, Celery, pytest, ruff, etc.) from `uv.lock`.
Activate it for the rest of this guide:

```powershell
.venv\Scripts\Activate.ps1
```

Every command below is also runnable without activating, via `uv run <cmd>`
(e.g. `uv run alembic upgrade head`) — both are used interchangeably in this
project's own scripts and skills.

## 3. PostgreSQL (local service, no Docker)

Install PostgreSQL 18 as a local Windows service
(`postgresql-x64-18`) if you don't already have it. Using `psql` or pgAdmin,
create a database and a role matching what you'll put in `.env`:

```sql
CREATE DATABASE job_board;
-- If you don't already have a suitable role:
CREATE ROLE postgres WITH LOGIN PASSWORD 'password';
GRANT ALL PRIVILEGES ON DATABASE job_board TO postgres;
```

Adjust the name/password to taste — just keep them consistent with the
`DATABASE_URL` you set in step 5.

## 4. Memurai (Redis for Windows)

Install [Memurai](https://www.memurai.com/) and make sure its service is
running on the default port 6379. This project uses Redis for three
independent purposes, all on the *same* Memurai instance but different
logical DB indexes: the Celery broker/result backend, API rate-limit
counters, and the `/api/v1/jobs/filters` response cache. `.env.example`
documents the DB-index env vars for each; you don't need to change them for
local development.

## 5. Create your `.env`

```powershell
Copy-Item .env.example .env
```

Open `.env` and fill in at minimum:

- `DATABASE_URL` — must match the database/role you created in step 3, e.g.
  `postgresql://postgres:password@localhost:5432/job_board`
- `JWT_SECRET` — any long random string for local dev
- `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` — only required if you're
  testing Google OAuth login locally; the job catalogue API itself doesn't
  need them

Everything else in `.env.example` has a sane default for local development.

## 6. Run database migrations

```powershell
uv run alembic upgrade head
```

This creates every table (`providers`, `jobs`, `job_staging`, `import_runs`,
auth tables, etc.) against the empty database from step 3. To generate a new
migration after changing `app/db/models.py`, use
`uv run alembic revision --autogenerate -m "<summary>"` and always read the
generated file before applying — autogenerate reliably catches columns and
indexes but not `CHECK` constraints or comments.

## 7. Seed realistic dev data

```powershell
uv run python scripts/seed_dev_data.py
```

This creates the `jobg8` provider row (if it doesn't already exist) and
inserts 200 synthetic jobs directly into the `jobs` table — built by running
fake-but-realistic feed records through the *same* validation schema and
inference logic a real Jobg8 import uses, so the data is shaped exactly like
production output: varied titles/descriptions/locations, a valid HTTP(S)
`job_url` on every job (jobs without one are never live, matching the real
spec), a wide mix of `classification`/`employment_type`/`country_name`
values so `/api/v1/jobs/filters` has real facets to return, and a mix of
active and expired (`is_active=false`, `deactivated_at` set) jobs so you can
see both states in the frontend.

Pass `--count` to change how many jobs are generated (default 200):

```powershell
uv run python scripts/seed_dev_data.py --count 500
```

Re-running the script is safe — it's idempotent by clear-and-reseed: it
deletes every job it previously created (tagged with a `SEED-` prefixed
`source_job_id`) and inserts a fresh batch. It never touches jobs from a
real import, and it refuses to run at all unless `DATABASE_URL` points at
`localhost`/`127.0.0.1`, so it can never be pointed at a shared or
production database.

## 8. Start the API

```powershell
uv run uvicorn app.main:app --reload
```

The API is now serving at `http://localhost:8000`. Interactive docs are at
`http://localhost:8000/docs`.

## 9. Start a Celery worker (optional for basic frontend work)

```powershell
uv run celery -A app.celery_app worker --loglevel=info --pool=solo
```

`--pool=solo` is required on Windows — the default prefork pool doesn't
work here. **This is optional** if you're just testing the frontend or API
against seeded data: the seed script writes directly to `jobs` and doesn't
involve Celery at all. You only need a worker running if you're testing the
actual import/promotion pipeline or scheduled tasks.

## 10. Verify it worked

```powershell
# Liveness/readiness (readiness checks Postgres + Redis)
Invoke-RestMethod http://localhost:8000/health
Invoke-RestMethod http://localhost:8000/health/ready

# Should return the seeded jobs
Invoke-RestMethod "http://localhost:8000/api/v1/jobs?limit=5"

# Should return populated facets from the seeded data
Invoke-RestMethod http://localhost:8000/api/v1/jobs/filters
```

`/health/ready` should return `{"status": "ok"}`. `/api/v1/jobs` should
return a page of jobs whose `job_url` starts with
`https://careers.example.test/jobs/...` — that's the seed script's fake
apply URL, confirming you're looking at seeded data. `/api/v1/jobs/filters`
should list several `classifications`, `employment_types`, and
`country_names` with non-zero counts.

If you get an empty `items` list, re-run step 7. If `/health/ready` returns
503, check that your PostgreSQL service and Memurai are both running.
