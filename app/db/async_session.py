"""Asynchronous PostgreSQL engine and session configuration for the API layer."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

load_dotenv(Path(__file__).resolve().parents[2] / ".env", encoding="utf-8-sig")


def _async_database_url() -> str:
    """Return DATABASE_URL normalized to SQLAlchemy's asyncpg dialect."""
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError(
            "DATABASE_URL must be configured before using the async database layer"
        )

    supported_prefixes = (
        "postgres://",
        "postgresql://",
        "postgresql+psycopg://",
    )
    for prefix in supported_prefixes:
        if database_url.startswith(prefix):
            return database_url.replace(prefix, "postgresql+asyncpg://", 1)
    if database_url.startswith("postgresql+asyncpg://"):
        return database_url
    raise RuntimeError("DATABASE_URL must use a PostgreSQL URL")


async_engine: AsyncEngine = create_async_engine(
    _async_database_url(),
    pool_pre_ping=True,
)

AsyncSessionLocal = async_sessionmaker(
    bind=async_engine,
    class_=AsyncSession,
    autoflush=False,
    expire_on_commit=False,
)
