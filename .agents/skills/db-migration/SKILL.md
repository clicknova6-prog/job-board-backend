---
name: db-migration
description: Use when a SQLAlchemy model change in app/db/models.py needs an Alembic migration in this project. Covers the exact commands and this project's DATABASE_URL wiring.
---

- `DATABASE_URL` comes from the repo-root `.env`, loaded by `app/db/session.py`. `alembic/env.py` reuses that same `_database_url()` helper, so `postgres://`/`postgresql://` both normalize to the psycopg driver automatically — nothing to configure per migration.
- A new class added to `app/db/models.py` is picked up automatically (`alembic/env.py` imports that module wholesale); you only need to touch `env.py` if you add a new models module entirely.
- Generate: `uv run alembic revision --autogenerate -m "<summary>"`. Apply: `uv run alembic upgrade head`.
- Always read the generated file before applying. Autogenerate reliably catches columns and indexes but not `CHECK` constraints or table/column comments — this project's models lean heavily on both.
- Match the existing granularity: one small, purpose-named migration per logical change (see `alembic/versions/` — e.g. "add_import_run_unmapped_fields"), not a batched schema dump.
