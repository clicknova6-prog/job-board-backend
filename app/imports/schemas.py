"""Validation schemas for records parsed from the Jobg8 XML feed.

The importer should pass one parsed ``<Job>`` element to ``JobFeedRecord``.
Field aliases intentionally match the XML element names, while Python attribute
names use the project's snake_case naming convention.
"""

import re
from decimal import Decimal
from typing import Annotated, Any, Self

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    model_validator,
)

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


# Reusing the annotated types above as adapters keeps the fallback logic and
# the declared field constraints from drifting apart: a value is only coerced
# to None after failing the exact rule the field itself would have applied.
_SALARY_ADAPTER = TypeAdapter(SalaryAmount)
_SELL_PRICE_ADAPTER = TypeAdapter(SellPriceAmount)
_URL_ADAPTER = TypeAdapter(AnyHttpUrl)

_CLEAN_CURRENCY_CODE = re.compile(r"^[A-Za-z]{3}$")
_CURRENCY_SEPARATORS = re.compile(r"[\s.]+")

# Model-internal keys that are not feed elements and must never be captured
# into the preserved source record.
_CONTROL_KEYS = frozenset({"fallback_fields", "source_record"})


def normalize_currency_code(raw: str) -> str | None:
    """Extract a three-letter currency code from a feed-supplied value.

    The feed does not always send a bare ISO code. Real Jobg8 snapshots use a
    ``"<Currency Name> . <CODE>"`` form such as ``"US Dollar . USD"``, so the
    trailing token is checked when the value is not already a clean code.

    Args:
        raw: The value exactly as supplied by the feed.

    Returns:
        The uppercased three-letter code, or None when no code can be found.
    """
    value = raw.strip()
    if not value:
        return None

    if _CLEAN_CURRENCY_CODE.match(value):
        return value.upper()

    tokens = [token for token in _CURRENCY_SEPARATORS.split(value) if token]
    if tokens and _CLEAN_CURRENCY_CODE.match(tokens[-1]):
        return tokens[-1].upper()

    return None


def _is_lenient_http_url(value: str) -> bool:
    """Accept a URL that strict parsing rejected but that is still usable.

    Only applied to apply_url, which cannot be dropped: the record is useless
    without somewhere to send a candidate.
    """
    return value.startswith(("http://", "https://")) and not any(
        character.isspace() for character in value
    )


class JobFeedRecord(BaseModel):
    """One ``<Job>`` record from the Jobg8 XML snapshot."""

    model_config = ConfigDict(
        populate_by_name=True,
        str_strip_whitespace=True,
        # The feed may add elements at any time. Capturing them in model_extra
        # keeps an unrecognized tag from rejecting an otherwise valid job, and
        # lets the importer report which unmapped fields a run encountered.
        extra="allow",
    )

    advertiser_name: str | None = Field(
        default=None,
        alias="AdvertiserName",
        description="Name of the employer, recruiter, or advertiser.",
    )
    advertiser_type: str | None = Field(
        default=None,
        alias="AdvertiserType",
        description="Feed-supplied advertiser category, such as Agency.",
    )
    sender_reference: str = Field(
        alias="SenderReference",
        min_length=1,
        description="Unique Jobg8 identifier for this job and the upsert key within the provider.",
    )
    display_reference: str | None = Field(
        default=None,
        alias="DisplayReference",
        description="Human-facing job reference supplied by the provider.",
    )
    classification: str | None = Field(
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
    country_name: str | None = Field(
        default=None,
        alias="Country",
        description="Country name exactly as supplied; the feed does not provide a country code.",
    )
    location: str | None = Field(
        default=None,
        alias="Location",
        description="Free-form location, which may be a city, region, or Remote.",
    )
    area: str | None = Field(
        default=None,
        alias="Area",
        description="Broader geographic area supplied by the feed.",
    )
    postal_code: str | None = Field(
        default=None,
        alias="PostalCode",
        description="Postal code stored as text to preserve leading zeros and non-numeric values.",
    )
    apply_url: str = Field(
        alias="ApplicationURL",
        min_length=1,
        description=(
            "Required HTTP or HTTPS destination for a candidate application. "
            "Normalized by apply_field_fallbacks, which stores the strictly "
            "parsed form when possible and a leniently accepted raw string "
            "otherwise; a value failing both checks still rejects the record."
        ),
    )
    language_code: str | None = Field(
        default=None,
        alias="Language",
        description="Provider language code, for example 2057; it is retained as text, not inferred as an ISO code.",
    )
    employment_type: str | None = Field(
        default=None,
        alias="EmploymentType",
        description="Employment arrangement, such as Full Time or Contract.",
    )
    start_date_text: str | None = Field(
        default=None,
        alias="StartDate",
        description="Source start-date wording, stored as text because it can be Immediate rather than a date.",
    )
    duration: str | None = Field(
        default=None,
        alias="Duration",
        description="Source duration wording, such as Permanent or a contract term.",
    )
    work_hours: str | None = Field(
        default=None,
        alias="WorkHours",
        description="Working-hours wording, such as Full Time or Part Time.",
    )
    salary_currency: CurrencyCode | None = Field(
        default=None,
        alias="SalaryCurrency",
        description="Three-letter currency code for the salary values.",
    )
    salary_min: SalaryAmount | None = Field(
        default=None,
        alias="SalaryMinimum",
        description="Optional lower salary amount.",
    )
    salary_max: SalaryAmount | None = Field(
        default=None,
        alias="SalaryMaximum",
        description="Optional upper salary amount.",
    )
    salary_period: str | None = Field(
        default=None,
        alias="SalaryPeriod",
        description="Salary frequency, such as Monthly or Annual.",
    )
    salary_additional: str | None = Field(
        default=None,
        alias="SalaryAdditional",
        description="Additional salary or benefits text supplied by the provider.",
    )
    advertiser_logo_url: str | None = Field(
        default=None,
        alias="LogoURL",
        description=(
            "Optional HTTP or HTTPS advertiser logo URL, dropped to None "
            "rather than rejecting the record when it does not parse."
        ),
    )
    job_type: str | None = Field(
        default=None,
        alias="JobType",
        description="Provider job category, for example TRAFFIC or ATS.",
    )

    # These fields are validated because they occur in every XML record, but
    # they deliberately have no dedicated PostgreSQL columns. The importer
    # keeps them in source_payload with the complete original record.
    sell_price: SellPriceAmount | None = Field(
        default=None,
        alias="SellPrice",
        description="Optional provider commercial price; retained only in source_payload.",
    )
    sell_price_currency: CurrencyCode | None = Field(
        default=None,
        alias="SellPriceCurrency",
        description="Currency of SellPrice; retained only in source_payload.",
    )
    revenue_type: str | None = Field(
        default=None,
        alias="RevenueType",
        description="Provider commercial revenue classification; retained only in source_payload.",
    )

    # Not a feed field. exclude=True keeps it out of model_dump(), so it never
    # reaches source_payload or the payload hash; the importer reads it to
    # report which fields needed fallback handling during a run.
    fallback_fields: set[str] = Field(
        default_factory=set,
        exclude=True,
        description="Names of fields whose value required fallback handling.",
    )

    # The feed's own record, captured before any coercion. The typed fields
    # above hold normalized values for the relational columns, while this keeps
    # what the provider actually sent for source_payload. Also exclude=True, so
    # it never nests itself inside a model_dump().
    source_record: dict[str, Any] = Field(
        default_factory=dict,
        exclude=True,
        description="Original feed element names and values, exactly as supplied.",
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

        # Non-string values are passed through untouched: apply_field_fallbacks
        # substitutes already-coerced values (Decimal, the fallback_fields set)
        # that must survive whichever of the two validators runs second.
        return {
            key: ((item.strip() or None) if isinstance(item, str) else item)
            for key, item in value.items()
        }

    @model_validator(mode="before")
    @classmethod
    def apply_field_fallbacks(cls, value: object) -> object:
        """Coerce malformed non-essential values instead of rejecting the record.

        Every branch treats a missing or blank element as simply absent, so
        this validator does not depend on running before or after
        ``blank_strings_are_missing``. A field is only flagged when a value
        was actually supplied and did not match what the schema expects.
        """
        if not isinstance(value, dict):
            return value

        data = dict(value)

        # Snapshot the feed's values before anything below rewrites them. This
        # runs ahead of every coercion, so source_record holds the provider's
        # original strings even for fields normalized further down.
        data["source_record"] = {
            key: item for key, item in data.items() if key not in _CONTROL_KEYS
        }

        fallback: set[str] = set(data.get("fallback_fields") or ())

        def supplied(alias: str) -> str | None:
            """Return the trimmed value for an alias, or None when absent."""
            raw = data.get(alias)
            if not isinstance(raw, str):
                return None
            trimmed = raw.strip()
            return trimmed or None

        for field_name, alias in (
            ("salary_currency", "SalaryCurrency"),
            ("sell_price_currency", "SellPriceCurrency"),
        ):
            raw_value = supplied(alias)
            if raw_value is None:
                continue
            code = normalize_currency_code(raw_value)
            # A value that was already a clean code is not a fallback, even
            # though it may have been uppercased.
            if code is None or code != raw_value.upper():
                fallback.add(field_name)
            data[alias] = code

        for field_name, alias, adapter in (
            ("salary_min", "SalaryMinimum", _SALARY_ADAPTER),
            ("salary_max", "SalaryMaximum", _SALARY_ADAPTER),
            ("sell_price", "SellPrice", _SELL_PRICE_ADAPTER),
        ):
            raw_value = supplied(alias)
            if raw_value is None:
                continue
            try:
                data[alias] = adapter.validate_python(raw_value)
            except ValidationError:
                data[alias] = None
                fallback.add(field_name)

        logo_url = supplied("LogoURL")
        if logo_url is not None:
            try:
                data["LogoURL"] = str(_URL_ADAPTER.validate_python(logo_url))
            except ValidationError:
                data["LogoURL"] = None
                fallback.add("advertiser_logo_url")

        # apply_url is essential, so it is never silently dropped: a value that
        # fails even the lenient check becomes None and rejects the record.
        application_url = supplied("ApplicationURL")
        if application_url is not None:
            try:
                data["ApplicationURL"] = str(
                    _URL_ADAPTER.validate_python(application_url)
                )
            except ValidationError:
                fallback.add("apply_url")
                data["ApplicationURL"] = (
                    application_url if _is_lenient_http_url(application_url) else None
                )

        data["fallback_fields"] = fallback
        return data

    @model_validator(mode="after")
    def salary_range_is_valid(self) -> Self:
        """Drop an inverted salary range rather than rejecting the record.

        The bounds cannot be trusted once they contradict each other, but the
        job itself is still usable, so both are cleared and the record is
        flagged instead of raising.
        """
        if (
            self.salary_min is not None
            and self.salary_max is not None
            and self.salary_min > self.salary_max
        ):
            self.salary_min = None
            self.salary_max = None
            self.fallback_fields.add("salary_range")
        return self
