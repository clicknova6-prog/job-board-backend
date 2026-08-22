"""Request and response contracts for affiliate-link administration."""

from __future__ import annotations

from pydantic import BaseModel


class AffiliateLookupRequest(BaseModel):
    """Provider-scoped source references to resolve for an administrator."""

    provider_id: int
    source_job_ids: list[str]


class AffiliateLookupMatch(BaseModel):
    """One source reference matched to an internal job."""

    job_id: int
    source_job_id: str
    title: str
    advertiser_name: str | None
    internal_job_id: int
    apply_url_available: bool
    has_affiliate_link: bool
    existing_short_hash: str | None


class AffiliateLookupResponse(BaseModel):
    """Matched and unresolved source references."""

    matched: list[AffiliateLookupMatch]
    not_found: list[str]


class AffiliateGenerateRequest(BaseModel):
    """Provider-scoped internal jobs confirmed for link generation."""

    provider_id: int
    job_ids: list[int]


class AffiliateGeneratedLink(BaseModel):
    """One generated or pre-existing affiliate link."""

    job_id: int
    short_hash: str
    redirect_url: str


class AffiliateExcludedJob(BaseModel):
    """One requested job excluded during server-side revalidation."""

    job_id: int
    reason: str


class AffiliateGenerateResponse(BaseModel):
    """Generated links and jobs rejected during confirmation."""

    generated: list[AffiliateGeneratedLink]
    excluded: list[AffiliateExcludedJob]
