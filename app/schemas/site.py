"""Request and response contracts for site and routing-rule administration."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.db.site_repositories import origin_of

SLUG_PATTERN = r"^[a-z0-9]+(-[a-z0-9]+)*$"
RuleType = Literal["include_classification", "include_title", "exclude_title"]


def _validate_base_url(value: str) -> str:
    value = value.strip().rstrip("/")
    if origin_of(value) is None:
        raise ValueError("must be an absolute http(s) URL")
    return value


def _validate_config(value: dict[str, Any]) -> dict[str, Any]:
    origins = value.get("cors_origins")
    valid = origins is None or (
        isinstance(origins, list)
        and all(isinstance(o, str) and origin_of(o) is not None for o in origins)
    )
    if not valid:
        raise ValueError("cors_origins must be a list of absolute http(s) origins")
    return value


class SiteRead(BaseModel):
    """A site as administrators see it."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    slug: str
    name: str
    site_key: str
    base_url: str
    is_active: bool
    canonical_priority: int
    config: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    active_job_count: int = 0
    rule_count: int = 0


class SiteCreate(BaseModel):
    """Fields for a new site."""

    model_config = ConfigDict(extra="forbid")

    slug: str = Field(pattern=SLUG_PATTERN, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    site_key: str = Field(min_length=8, max_length=200, pattern=r"^\S+$")
    base_url: str
    is_active: bool = True
    canonical_priority: int = Field(
        default=0,
        ge=-1000,
        le=1000,
        description="Higher wins canonical ownership of jobs shared with other sites.",
    )
    config: dict[str, Any] = Field(default_factory=dict)

    _base_url = field_validator("base_url")(_validate_base_url)
    _config = field_validator("config")(_validate_config)


class SiteUpdate(BaseModel):
    """Fields that may be changed on an existing site (slug is immutable)."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    site_key: str | None = Field(
        default=None, min_length=8, max_length=200, pattern=r"^\S+$"
    )
    base_url: str | None = None
    is_active: bool | None = None
    canonical_priority: int | None = Field(default=None, ge=-1000, le=1000)
    config: dict[str, Any] | None = None

    @field_validator("base_url")
    @classmethod
    def _base_url(cls, value: str | None) -> str | None:
        return None if value is None else _validate_base_url(value)

    @field_validator("config")
    @classmethod
    def _config(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return None if value is None else _validate_config(value)


class RoutingRuleRead(BaseModel):
    """One routing rule as administrators see it."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    site_id: int
    provider_id: int | None
    rule_type: RuleType
    pattern: str
    is_active: bool
    notes: str | None
    created_at: datetime


class RoutingRuleDraft(BaseModel):
    """A rule definition, used both for creation and for dry-run previews."""

    model_config = ConfigDict(extra="forbid")

    rule_type: RuleType
    pattern: str = Field(min_length=1, max_length=500)
    provider_id: int | None = None
    is_active: bool = True
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("pattern")
    @classmethod
    def _pattern(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("pattern must not be blank")
        return value


class RoutingRuleUpdate(BaseModel):
    """Fields that may be changed on an existing rule."""

    model_config = ConfigDict(extra="forbid")

    rule_type: RuleType | None = None
    pattern: str | None = Field(default=None, min_length=1, max_length=500)
    provider_id: int | None = None
    is_active: bool | None = None
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("pattern")
    @classmethod
    def _pattern(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("pattern must not be blank")
        return value


class RoutingPreviewRequest(BaseModel):
    """Dry-run input. Omit ``rules`` to preview the site's saved active rules."""

    model_config = ConfigDict(extra="forbid")

    rules: list[RoutingRuleDraft] | None = Field(default=None, max_length=2000)
    sample_size: int = Field(default=10, ge=0, le=100)


class RulePreviewOut(BaseModel):
    """Match statistics for one rule in a preview."""

    model_config = ConfigDict(from_attributes=True)

    rule_id: int | None = Field(
        description="Saved rule id, or null for a draft rule in the request."
    )
    position: int = Field(description="Index of the rule in the evaluated list.")
    rule_type: RuleType
    pattern: str
    matched: int = Field(description="Jobs assigned by this rule (first match wins).")
    excluded: int = Field(
        description="For exclude rules: jobs it removed that would otherwise match."
    )
    sample_titles: list[str]


class RoutingPreviewOut(BaseModel):
    """What applying the rules would do to the site. Nothing is written."""

    model_config = ConfigDict(from_attributes=True)

    site_id: int
    jobs_evaluated: int
    matched: int
    active_matched: int
    currently_assigned: int
    would_add: int
    would_remove: int
    added_samples: list[str]
    removed_samples: list[str]
    rules: list[RulePreviewOut]
