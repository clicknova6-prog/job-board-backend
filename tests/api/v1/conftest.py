"""Test database bootstrap for the public job API integration tests.

These tests exercise real PostgreSQL behaviour -- the ``search_vector``
generated column, GIN full-text matching, and row-wise keyset comparisons --
so they run against a real database rather than a stand-in. A dedicated
``job_board_test`` database is created and migrated once per session; the
developer database is never touched.

Override the target with ``TEST_DATABASE_URL`` if the default derivation
(``DATABASE_URL`` with the database name replaced by ``job_board_test``) is
not what you want.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from dotenv import load_dotenv
from psycopg import sql

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_TEST_DATABASE_NAME = "job_board_test"

load_dotenv(REPO_ROOT / ".env", encoding="utf-8-sig")


def _sync_test_database_url() -> str:
    """Return the psycopg URL of the database these tests may freely mutate."""
    configured = os.environ.get("TEST_DATABASE_URL")
    if configured:
        return configured

    developer_url = os.environ.get("DATABASE_URL")
    if not developer_url:
        pytest.skip("DATABASE_URL or TEST_DATABASE_URL must be set for API DB tests")

    parts = urlsplit(developer_url)
    return urlunsplit(parts._replace(path=f"/{DEFAULT_TEST_DATABASE_NAME}"))


def _create_database_if_missing(url: str) -> None:
    """Create the test database, connecting via the ``postgres`` maintenance DB."""
    parts = urlsplit(url)
    database_name = parts.path.lstrip("/")
    maintenance_url = urlunsplit(parts._replace(path="/postgres"))

    with psycopg.connect(maintenance_url, autocommit=True) as connection:
        exists = connection.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (database_name,)
        ).fetchone()
        if exists is None:
            # Identifiers cannot be parameterized; the name comes from local
            # configuration, not from request data.
            connection.execute(
                sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name))
            )


@pytest.fixture(scope="session")
def test_database_url() -> Iterator[str]:
    """Ensure a migrated test database exists and yield its URL."""
    url = _sync_test_database_url()

    try:
        _create_database_if_missing(url)
    except psycopg.OperationalError as exc:  # pragma: no cover - env dependent
        pytest.skip(f"PostgreSQL is not reachable for API DB tests: {exc}")

    # alembic/env.py reads DATABASE_URL, and python-dotenv does not override
    # real environment variables, so this cleanly retargets the migration run.
    completed = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        env={**os.environ, "DATABASE_URL": url},
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:  # pragma: no cover - surfaces setup failures
        pytest.fail(
            "alembic upgrade head failed for the test database:\n"
            f"{completed.stdout}\n{completed.stderr}"
        )

    yield url
