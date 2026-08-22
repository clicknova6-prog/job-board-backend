"""Environment-backed settings for authentication services."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env", encoding="utf-8-sig")


def _required_environment_value(name: str) -> str:
    """Return a non-empty environment value or raise a configuration error."""
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise RuntimeError(f"{name} must be configured")
    return value


@dataclass(frozen=True, slots=True)
class JWTSettings:
    """JWT signing configuration."""

    secret: str
    algorithm: str

    @classmethod
    def from_environment(cls) -> JWTSettings:
        """Load required JWT settings from environment variables."""
        return cls(
            secret=_required_environment_value("JWT_SECRET"),
            algorithm=_required_environment_value("JWT_ALGORITHM"),
        )


@dataclass(frozen=True, slots=True)
class GoogleOAuthSettings:
    """Google OAuth client and callback configuration."""

    client_id: str
    client_secret: str
    redirect_uri: str

    @classmethod
    def from_environment(cls) -> GoogleOAuthSettings:
        """Load required Google OAuth settings from environment variables."""
        return cls(
            client_id=_required_environment_value("GOOGLE_CLIENT_ID"),
            client_secret=_required_environment_value("GOOGLE_CLIENT_SECRET"),
            redirect_uri=_required_environment_value("GOOGLE_OAUTH_REDIRECT_URI"),
        )
