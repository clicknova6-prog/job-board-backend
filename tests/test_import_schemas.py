"""Unit tests for Jobg8 feed-record validation and normalization."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from app.imports.schemas import (
    JobFeedRecord,
    normalize_currency_code,
    normalize_employment_type,
)


def _valid_record(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "SenderReference": "job-123",
        "Position": "Software Engineer",
        "Description": "Build and maintain software.",
        "ApplicationURL": "https://example.com/jobs/123",
    }
    record.update(overrides)
    return record


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("USD", "USD"),
        ("gbp", "GBP"),
        (" EUR ", "EUR"),
        ("US Dollar . USD", "USD"),
        ("Pound Sterling GBP", "GBP"),
        ("", None),
        ("   ", None),
        ("US Dollar", None),
        ("US Dollar . US", None),
    ],
)
def test_normalize_currency_code(raw: str, expected: str | None) -> None:
    assert normalize_currency_code(raw) == expected


@pytest.mark.parametrize(
    "validator",
    [
        JobFeedRecord.blank_strings_are_missing,
        JobFeedRecord.apply_field_fallbacks,
    ],
)
def test_before_validators_pass_non_mapping_input_through(validator: Any) -> None:
    value = "not a feed mapping"

    assert validator(value) is value


def test_blank_strings_become_missing_but_non_strings_are_unchanged() -> None:
    value = {
        "empty": "  ",
        "trimmed": "  content  ",
        "number": 42,
        "missing": None,
    }

    assert JobFeedRecord.blank_strings_are_missing(value) == {
        "empty": None,
        "trimmed": "content",
        "number": 42,
        "missing": None,
    }


def test_fully_valid_record_is_accepted_and_preserves_raw_source() -> None:
    raw = _valid_record(
        Position="  Software Engineer  ",
        NewProviderField="  provider value  ",
    )

    record = JobFeedRecord.model_validate(raw)

    assert record.sender_reference == "job-123"
    assert record.title == "Software Engineer"
    assert record.description == "Build and maintain software."
    assert record.apply_url == "https://example.com/jobs/123"
    assert record.fallback_fields == set()
    assert record.source_record == raw
    assert record.model_extra == {"NewProviderField": "provider value"}
    assert "fallback_fields" not in record.model_dump()
    assert "source_record" not in record.model_dump()


@pytest.mark.parametrize("missing_alias", ["ApplicationURL", "Position"])
def test_missing_essential_field_is_rejected(missing_alias: str) -> None:
    raw = _valid_record()
    raw.pop(missing_alias)

    with pytest.raises(ValidationError) as exc_info:
        JobFeedRecord.model_validate(raw)

    assert missing_alias in {error["loc"][0] for error in exc_info.value.errors()}


@pytest.mark.parametrize("blank_alias", ["SenderReference", "Position", "Description"])
def test_blank_required_text_field_is_rejected(blank_alias: str) -> None:
    with pytest.raises(ValidationError) as exc_info:
        JobFeedRecord.model_validate(_valid_record(**{blank_alias: "   "}))

    assert blank_alias in {error["loc"][0] for error in exc_info.value.errors()}


@pytest.mark.parametrize(
    ("source_value", "expected"),
    [
        ("FT", "full_time"),
        ("full_time", "full_time"),
        ("Full-time", "full_time"),
        ("full time", "full_time"),
        ("fulltime", "full_time"),
        ("Permanent", "full_time"),
        ("PT", "part_time"),
        ("part_time", "part_time"),
        ("Part-time", "part_time"),
        ("parttime", "part_time"),
        ("Contract", "contract"),
        ("contractor", "contract"),
        ("1099", "contract"),
        ("freelance", "contract"),
        ("freelancer", "contract"),
        ("Temporary", "temporary"),
        ("temp", "temporary"),
        ("seasonal", "temporary"),
        ("fixed-term", "temporary"),
        ("Internship", "internship"),
        ("intern", "internship"),
        ("Other", "other"),
        ("Any", "other"),
    ],
)
def test_employment_type_known_variants_are_normalized(
    source_value: str,
    expected: str,
) -> None:
    record = JobFeedRecord.model_validate(
        _valid_record(EmploymentType=f"  {source_value}  ")
    )

    assert record.employment_type == expected
    assert record.fallback_fields == set()


def test_unknown_employment_type_maps_to_other_and_preserves_raw_value() -> None:
    raw_value = "  bespoke engagement  "

    record = JobFeedRecord.model_validate(
        _valid_record(EmploymentType=raw_value)
    )

    assert record.employment_type == "other"
    assert record.fallback_fields == {"employment_type"}
    assert record.source_record["EmploymentType"] == raw_value


def test_blank_employment_type_is_missing_without_fallback() -> None:
    record = JobFeedRecord.model_validate(
        _valid_record(EmploymentType="   ")
    )

    assert record.employment_type is None
    assert record.fallback_fields == set()
    assert normalize_employment_type("   ") is None


def test_currency_fields_normalize_known_formats_and_flag_composite_values() -> None:
    record = JobFeedRecord.model_validate(
        _valid_record(
            SalaryCurrency="gbp",
            SellPriceCurrency="US Dollar . USD",
        )
    )

    assert record.salary_currency == "GBP"
    assert record.sell_price_currency == "USD"
    assert record.fallback_fields == {"sell_price_currency"}


@pytest.mark.parametrize(
    ("alias", "field_name"),
    [
        ("SalaryCurrency", "salary_currency"),
        ("SellPriceCurrency", "sell_price_currency"),
    ],
)
def test_unknown_currency_is_dropped_and_flagged(
    alias: str,
    field_name: str,
) -> None:
    record = JobFeedRecord.model_validate(_valid_record(**{alias: "not a currency"}))

    assert getattr(record, field_name) is None
    assert record.fallback_fields == {field_name}


def test_valid_amounts_are_coerced_to_decimals_without_fallbacks() -> None:
    record = JobFeedRecord.model_validate(
        _valid_record(
            SalaryMinimum="1000.50",
            SalaryMaximum="2000",
            SellPrice="1.2345",
        )
    )

    assert record.salary_min == Decimal("1000.50")
    assert record.salary_max == Decimal(2000)
    assert record.sell_price == Decimal("1.2345")
    assert record.fallback_fields == set()


@pytest.mark.parametrize(
    ("alias", "field_name", "invalid_value"),
    [
        ("SalaryMinimum", "salary_min", "-1"),
        ("SalaryMaximum", "salary_max", "not a number"),
        ("SellPrice", "sell_price", "1.23456"),
    ],
)
def test_invalid_amount_is_dropped_and_flagged(
    alias: str,
    field_name: str,
    invalid_value: str,
) -> None:
    record = JobFeedRecord.model_validate(
        _valid_record(**{alias: invalid_value})
    )

    assert getattr(record, field_name) is None
    assert record.fallback_fields == {field_name}


def test_blank_optional_fallback_fields_are_missing_without_being_flagged() -> None:
    record = JobFeedRecord.model_validate(
        _valid_record(
            SalaryCurrency="  ",
            SellPriceCurrency="  ",
            SalaryMinimum="  ",
            SalaryMaximum="  ",
            SellPrice="  ",
            LogoURL="  ",
        )
    )

    assert record.salary_currency is None
    assert record.sell_price_currency is None
    assert record.salary_min is None
    assert record.salary_max is None
    assert record.sell_price is None
    assert record.advertiser_logo_url is None
    assert record.fallback_fields == set()


def test_already_coerced_non_string_value_survives_fallback_processing() -> None:
    amount = Decimal("123.45")
    processed = JobFeedRecord.apply_field_fallbacks({"SalaryMinimum": amount})

    assert processed["SalaryMinimum"] is amount
    assert processed["source_record"] == {"SalaryMinimum": amount}


@pytest.mark.parametrize(
    ("logo_url", "expected", "expected_fallbacks"),
    [
        ("https://example.com/logo.png", "https://example.com/logo.png", set()),
        ("not-a-url", None, {"advertiser_logo_url"}),
    ],
)
def test_logo_url_is_normalized_or_dropped(
    logo_url: str,
    expected: str | None,
    expected_fallbacks: set[str],
) -> None:
    record = JobFeedRecord.model_validate(_valid_record(LogoURL=logo_url))

    assert record.advertiser_logo_url == expected
    assert record.fallback_fields == expected_fallbacks


def test_lenient_http_apply_url_is_preserved_and_flagged() -> None:
    application_url = "https://[invalid"

    record = JobFeedRecord.model_validate(
        _valid_record(ApplicationURL=application_url)
    )

    assert record.apply_url == application_url
    assert record.fallback_fields == {"apply_url"}


def test_strict_url_parser_percent_encodes_spaces() -> None:
    record = JobFeedRecord.model_validate(
        _valid_record(ApplicationURL="https://example.com/job path")
    )

    assert record.apply_url == "https://example.com/job%20path"
    assert record.fallback_fields == set()


@pytest.mark.parametrize(
    "application_url",
    [
        "https://[invalid path",
        "ftp://example.com/jobs/123",
        "not-a-url",
    ],
)
def test_unusable_apply_url_is_rejected(application_url: str) -> None:
    with pytest.raises(ValidationError) as exc_info:
        JobFeedRecord.model_validate(
            _valid_record(ApplicationURL=application_url)
        )

    assert "ApplicationURL" in {
        error["loc"][0] for error in exc_info.value.errors()
    }


def test_existing_fallback_fields_are_merged_and_not_captured_as_source_data() -> None:
    record = JobFeedRecord.model_validate(
        _valid_record(
            SalaryMinimum="invalid",
            fallback_fields={"earlier_fallback"},
            source_record={"stale": "snapshot"},
        )
    )

    assert record.fallback_fields == {"earlier_fallback", "salary_min"}
    assert record.source_record == {
        key: value
        for key, value in _valid_record(SalaryMinimum="invalid").items()
    }


@pytest.mark.parametrize(
    ("salary_min", "salary_max", "expected_min", "expected_max", "fallbacks"),
    [
        ("100", "200", Decimal(100), Decimal(200), set()),
        ("200", "100", None, None, {"salary_range"}),
    ],
)
def test_salary_range_validation(
    salary_min: str,
    salary_max: str,
    expected_min: Decimal | None,
    expected_max: Decimal | None,
    fallbacks: set[str],
) -> None:
    record = JobFeedRecord.model_validate(
        _valid_record(SalaryMinimum=salary_min, SalaryMaximum=salary_max)
    )

    assert record.salary_min == expected_min
    assert record.salary_max == expected_max
    assert record.fallback_fields == fallbacks
