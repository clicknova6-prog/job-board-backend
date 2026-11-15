"""Unit tests for the pure site routing matcher."""

from __future__ import annotations

import pytest

from app.sites.matcher import (
    EXCLUDE_TITLE,
    INCLUDE_CLASSIFICATION,
    INCLUDE_TITLE,
    RoutingRule,
    SiteMatcher,
    normalize,
)

IT, MED = 1, 2


def _matcher(*rules: RoutingRule, provider_id: int = 7) -> SiteMatcher:
    return SiteMatcher(rules, provider_id=provider_id)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Senior IT Support Engineer", " senior it support engineer "),
        ("C# / .NET Developer", " c# net developer "),
        ("I.T. & Communications", " i t communications "),
        ("", ""),
        (None, ""),
        ("---", ""),
    ],
)
def test_normalize(text: str | None, expected: str) -> None:
    assert normalize(text) == expected


def test_title_phrase_matches_whole_words_only() -> None:
    matcher = _matcher(RoutingRule(1, IT, INCLUDE_TITLE, "IT support"))

    assert matcher.match(title="Senior IT Support Engineer", classification=None) == {
        IT: 1
    }
    assert matcher.match(title="Quality Support Lead", classification=None) == {}
    assert matcher.match(title="Submit Support", classification=None) == {}


def test_classification_matches_exactly_and_case_insensitively() -> None:
    matcher = _matcher(
        RoutingRule(1, IT, INCLUDE_CLASSIFICATION, "I.T. & Communications")
    )

    assert matcher.match(title="Anything", classification="i.t. & communications") == {
        IT: 1
    }
    assert (
        matcher.match(title="Anything", classification="I.T. & Communications Extra")
        == {}
    )
    assert matcher.match(title="Anything", classification=None) == {}


def test_exclude_beats_every_include_for_that_site_only() -> None:
    matcher = _matcher(
        RoutingRule(1, IT, INCLUDE_CLASSIFICATION, "Engineering"),
        RoutingRule(2, IT, EXCLUDE_TITLE, "CNC"),
        RoutingRule(3, MED, INCLUDE_TITLE, "CNC programmer"),
    )

    assert matcher.match(title="CNC Programmer", classification="Engineering") == {
        MED: 3
    }


def test_classification_rule_takes_precedence_over_title_rule() -> None:
    matcher = _matcher(
        RoutingRule(5, IT, INCLUDE_TITLE, "developer"),
        RoutingRule(9, IT, INCLUDE_CLASSIFICATION, "I.T. & Communications"),
    )

    assert matcher.match(
        title="Java Developer", classification="I.T. & Communications"
    ) == {IT: 9}


def test_first_title_rule_by_id_wins() -> None:
    matcher = _matcher(
        RoutingRule(8, IT, INCLUDE_TITLE, "devops"),
        RoutingRule(3, IT, INCLUDE_TITLE, "engineer"),
    )

    assert matcher.match(title="DevOps Engineer", classification=None) == {IT: 3}


def test_job_can_join_multiple_sites() -> None:
    matcher = _matcher(
        RoutingRule(1, IT, INCLUDE_TITLE, "health IT"),
        RoutingRule(2, MED, INCLUDE_CLASSIFICATION, "Healthcare & Medical"),
    )

    assert matcher.match(
        title="Health IT Analyst", classification="Healthcare & Medical"
    ) == {IT: 1, MED: 2}


def test_provider_scoped_rules_apply_only_to_their_provider() -> None:
    rules = (
        RoutingRule(1, IT, INCLUDE_TITLE, "developer", provider_id=7),
        RoutingRule(2, IT, INCLUDE_TITLE, "analyst", provider_id=None),
    )

    assert _matcher(*rules, provider_id=7).match(
        title="Developer", classification=None
    ) == {IT: 1}
    assert (
        _matcher(*rules, provider_id=8).match(title="Developer", classification=None)
        == {}
    )
    assert _matcher(*rules, provider_id=8).match(
        title="Analyst", classification=None
    ) == {IT: 2}


def test_blank_patterns_are_ignored_and_unknown_types_rejected() -> None:
    matcher = _matcher(RoutingRule(1, IT, INCLUDE_TITLE, " ... "))
    assert matcher.site_ids == ()
    assert matcher.match(title="Anything", classification=None) == {}

    with pytest.raises(ValueError, match="Unknown site routing rule type"):
        RoutingRule(1, IT, "include_description", "x")
