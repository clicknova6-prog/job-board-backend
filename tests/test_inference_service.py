"""Tests for pure job metadata inference rules."""

from __future__ import annotations

import pytest

from app.services.inference_service import (
    infer_experience_level,
    infer_remote_status,
)


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Remote Software Engineer", "remote"),
        ("Hybrid Software Engineer", "hybrid"),
        ("Onsite Software Engineer", "onsite"),
        ("On-site Software Engineer", "onsite"),
        ("Work From Home Nurse", "remote"),
        ("In-Office Accountant", "onsite"),
    ],
)
def test_infer_remote_status_from_each_title_keyword(
    title: str,
    expected: str,
) -> None:
    assert infer_remote_status(title, "") == (expected, "inferred")


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("fully remote", "remote"),
        ("100% remote", "remote"),
        ("remote position", "remote"),
        ("remote opportunity", "remote"),
        ("work from home", "remote"),
        ("hybrid role", "hybrid"),
        ("hybrid position", "hybrid"),
        ("hybrid schedule", "hybrid"),
        ("hybrid work", "hybrid"),
        ("onsite role", "onsite"),
        ("on-site role", "onsite"),
        ("onsite position", "onsite"),
        ("on-site position", "onsite"),
        ("in-office", "onsite"),
    ],
)
def test_infer_remote_status_from_each_description_phrase(
    phrase: str,
    expected: str,
) -> None:
    description = f"This is a {phrase} with a growing team."
    assert infer_remote_status("Software Engineer", description) == (
        expected,
        "inferred",
    )


def test_remote_status_is_case_insensitive() -> None:
    assert infer_remote_status("HYBRID ENGINEER", "") == ("hybrid", "inferred")
    assert infer_remote_status("Engineer", "A FULLY REMOTE POSITION") == (
        "remote",
        "inferred",
    )


@pytest.mark.parametrize(
    ("title", "description"),
    [
        ("Remote Monitoring Technician", "Monitors patient devices."),
        ("Monitoring Technician", "Provides remote monitoring for patients."),
    ],
)
def test_remote_monitoring_is_not_a_remote_work_signal(
    title: str,
    description: str,
) -> None:
    assert infer_remote_status(title, description) == (None, None)


def test_remote_title_takes_priority_over_description() -> None:
    assert infer_remote_status("Hybrid Engineer", "This is a fully remote role.") == (
        "hybrid",
        "inferred",
    )


def test_remote_status_no_match() -> None:
    assert infer_remote_status("Software Engineer", "Works with a local team.") == (
        None,
        None,
    )


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Senior Software Engineer", "senior"),
        ("Sr. Software Engineer", "senior"),
        ("Sr Software Engineer", "senior"),
        ("Junior Software Engineer", "entry"),
        ("Jr. Software Engineer", "entry"),
        ("Jr Software Engineer", "entry"),
        ("Entry Level Software Engineer", "entry"),
        ("Entry-Level Software Engineer", "entry"),
        ("Principal Software Engineer", "lead"),
        ("Lead Software Engineer", "lead"),
        ("Technical Lead", "lead"),
    ],
)
def test_infer_experience_level_from_each_title_keyword(
    title: str,
    expected: str,
) -> None:
    assert infer_experience_level(title, "") == (expected, "inferred")


def test_experience_level_is_case_insensitive() -> None:
    assert infer_experience_level("SENIOR ENGINEER", "") == (
        "senior",
        "inferred",
    )
    assert infer_experience_level("TECHNICAL LEAD", "") == ("lead", "inferred")


@pytest.mark.parametrize(
    "title",
    [
        "Senior Care Coordinator",
        "Senior Living Advisor",
        "Medical Staff Coordinator",
        "Lead Exposure Specialist",
        "Blood Lead Level Technician",
        "Lead-Free Manufacturing Engineer",
        "Engineer for lead exposure prevention",
    ],
)
def test_experience_level_false_positive_resistance(title: str) -> None:
    assert infer_experience_level(title, "") == (None, None)


def test_experience_level_does_not_scan_description() -> None:
    description = "Seeking a senior principal engineer with years of experience."
    assert infer_experience_level("Software Engineer", description) == (None, None)


def test_experience_level_priority_uses_first_rule() -> None:
    assert infer_experience_level("Senior Principal Engineer", "") == (
        "senior",
        "inferred",
    )


def test_experience_level_no_match_and_never_defaults_to_mid() -> None:
    assert infer_experience_level("Software Engineer", "") == (None, None)
