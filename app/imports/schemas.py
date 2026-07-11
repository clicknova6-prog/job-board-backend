"""Validation schemas for records parsed from the Jobg8 XML feed.

The importer should pass one parsed ``<Job>`` element to ``JobFeedRecord``.
Field aliases intentionally match the XML element names, while Python attribute
names use the project's snake_case naming convention.
"""

from decimal import Decimal
from typing import Annotated, Optional, Self

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, StringConstraints, model_validator


CurrencyCode = Annotated[
    str,
    StringConstraints(min_length=3, max_length=3, to_upper=True),
]
"""A three-letter currency code as supplied by the feed."""

SalaryAmount = Annotated[
    Decimal,
    Field(ge=0, max_digits=14, decimal_places=2),
]
"""A non-negative salary amount suitable for PostgreSQL numeric(14, 2)."""

SellPriceAmount = Annotated[
    Decimal,
    Field(ge=0, max_digits=14, decimal_places=4),
]
"""A non-negative provider price; it remains source-payload-only."""


class JobFeedRecord(BaseModel):
    """One ``<Job>`` record from the Jobg8 XML snapshot."""

    model_config = ConfigDict(
        populate_by_name=True,
        str_strip_whitespace=True,
        extra="forbid",
    )

    advertiser_name: Optional[str] = Field(
        default=None,
        alias="AdvertiserName",
        description="Name of the employer, recruiter, or advertiser.",
    )
    advertiser_type: Optional[str] = Field(
        default=None,
        alias="AdvertiserType",
        description="Feed-supplied advertiser category, such as Agency.",
    )
    sender_reference: str = Field(
        alias="SenderReference",
        min_length=1,
        description="Unique Jobg8 identifier for this job and the upsert key within the provider.",
    )
    display_reference: Optional[str] = Field(
        default=None,
        alias="DisplayReference",
        description="Human-facing job reference supplied by the provider.",
    )
    classification: Optional[str] = Field(
        default=None,
        alias="Classification",
        description="Primary job category supplied by the feed.",
    )
    title: str = Field(
        alias="Position",
        min_length=1,
        description="Job title from the Position XML element.",
    )
    description: str = Field(
        alias="Description",
        min_length=1,
        description="Full job description; text supports large CDATA content.",
    )
    country_name: Optional[str] = Field(
        default=None,
        alias="Country",
        description="Country name exactly as supplied; the feed does not provide a country code.",
    )
    location: Optional[str] = Field(
        default=None,
        alias="Location",
        description="Free-form location, which may be a city, region, or Remote.",
    )
    area: Optional[str] = Field(
        default=None,
        alias="Area",
        description="Broader geographic area supplied by the feed.",
    )
    postal_code: Optional[str] = Field(
        default=None,
        alias="PostalCode",
        description="Postal code stored as text to preserve leading zeros and non-numeric values.",
    )
    apply_url: AnyHttpUrl = Field(
        alias="ApplicationURL",
        description="Required HTTP or HTTPS destination for a candidate application.",
    )
    language_code: Optional[str] = Field(
        default=None,
        alias="Language",
        description="Provider language code, for example 2057; it is retained as text, not inferred as an ISO code.",
    )
    employment_type: Optional[str] = Field(
        default=None,
        alias="EmploymentType",
        description="Employment arrangement, such as Full Time or Contract.",
    )
    start_date_text: Optional[str] = Field(
        default=None,
        alias="StartDate",
        description="Source start-date wording, stored as text because it can be Immediate rather than a date.",
    )
    duration: Optional[str] = Field(
        default=None,
        alias="Duration",
        description="Source duration wording, such as Permanent or a contract term.",
    )
    work_hours: Optional[str] = Field(
        default=None,
        alias="WorkHours",
        description="Working-hours wording, such as Full Time or Part Time.",
    )
    salary_currency: Optional[CurrencyCode] = Field(
        default=None,
        alias="SalaryCurrency",
        description="Three-letter currency code for the salary values.",
    )
    salary_min: Optional[SalaryAmount] = Field(
        default=None,
        alias="SalaryMinimum",
        description="Optional lower salary amount.",
    )
    salary_max: Optional[SalaryAmount] = Field(
        default=None,
        alias="SalaryMaximum",
        description="Optional upper salary amount.",
    )
    salary_period: Optional[str] = Field(
        default=None,
        alias="SalaryPeriod",
        description="Salary frequency, such as Monthly or Annual.",
    )
    salary_additional: Optional[str] = Field(
        default=None,
        alias="SalaryAdditional",
        description="Additional salary or benefits text supplied by the provider.",
    )
    advertiser_logo_url: Optional[AnyHttpUrl] = Field(
        default=None,
        alias="LogoURL",
        description="Optional HTTP or HTTPS advertiser logo URL.",
    )
    job_type: Optional[str] = Field(
        default=None,
        alias="JobType",
        description="Provider job category, for example TRAFFIC or ATS.",
    )

    # These fields are validated because they occur in every XML record, but
    # they deliberately have no dedicated PostgreSQL columns. The importer
    # keeps them in source_payload with the complete original record.
    sell_price: Optional[SellPriceAmount] = Field(
        default=None,
        alias="SellPrice",
        description="Optional provider commercial price; retained only in source_payload.",
    )
    sell_price_currency: Optional[CurrencyCode] = Field(
        default=None,
        alias="SellPriceCurrency",
        description="Currency of SellPrice; retained only in source_payload.",
    )
    revenue_type: Optional[str] = Field(
        default=None,
        alias="RevenueType",
        description="Provider commercial revenue classification; retained only in source_payload.",
    )

    @model_validator(mode="before")
    @classmethod
    def blank_strings_are_missing(cls, value: object) -> object:
        """Treat empty XML elements as missing optional values.

        Required blank values become ``None`` and therefore fail normal
        required-field validation instead of being accepted as empty strings.
        """
        if not isinstance(value, dict):
            return value

        return {
            key: item.strip() if isinstance(item, str) and item.strip() else None
            for key, item in value.items()
        }

    @model_validator(mode="after")
    def salary_range_is_valid(self) -> Self:
        """Reject an inverted salary range when both bounds are supplied."""
        if (
            self.salary_min is not None
            and self.salary_max is not None
            and self.salary_min > self.salary_max
        ):
            raise ValueError("SalaryMinimum must not be greater than SalaryMaximum")
        return self
