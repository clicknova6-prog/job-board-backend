"""Synchronous PostgreSQL engine and session configuration."""

import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

# Load the repo-root .env before any code reads DATABASE_URL, so scripts and
# alembic pick it up without a manual export. utf-8-sig is required because
# the file carries a UTF-8 BOM, which would otherwise become part of the
# first key name. Real environment variables still win over .env values.
load_dotenv(Path(__file__).resolve().parents[2] / ".env", encoding="utf-8-sig")


def _database_url() -> str:
    """Read DATABASE_URL and select the installed psycopg driver.

    Supporting the common ``postgresql://`` spelling keeps local deployment
    configuration simple while still using psycopg 3 explicitly.
    """
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError(
            "DATABASE_URL must be configured before using the database layer"
        )

    if database_url.startswith("postgres://"):
        return database_url.replace("postgres://", "postgresql+psycopg://", 1)
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+psycopg://", 1)
    return database_url


# pool_pre_ping detects stale PostgreSQL connections before application code
# receives one. This module intentionally exposes no FastAPI dependency.
engine: Engine = create_engine(_database_url(), pool_pre_ping=True)

# Keeping instances usable after commit is convenient for service-layer code;
# transaction boundaries will be introduced with repositories later.
SessionLocal = sessionmaker(
    bind=engine, class_=Session, autoflush=False, expire_on_commit=False
)
