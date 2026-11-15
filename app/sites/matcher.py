"""Pure, provider-agnostic site routing rule matching.

No database access here: the matcher takes plain rule and job values so it is
trivially unit-testable and reusable by the routing service, preview tooling
and any future admin "dry run" endpoint.

Matching is whole-word/phrase based on a normalized form of the text:
lowercase, every character other than ``a-z0-9+#`` becomes a space, runs of
spaces collapse. ``"IT support"`` therefore matches "Senior IT Support
Engineer" but never "Quality"; ``"C#"`` and ``"C++"`` survive normalization.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

INCLUDE_CLASSIFICATION = "include_classification"
INCLUDE_TITLE = "include_title"
EXCLUDE_TITLE = "exclude_title"
RULE_TYPES = frozenset({INCLUDE_CLASSIFICATION, INCLUDE_TITLE, EXCLUDE_TITLE})

_NON_WORD = re.compile(r"[^a-z0-9+#]+")


def normalize(text: str | None) -> str:
    """Return ``text`` as a space-padded normalized phrase haystack/needle."""
    if not text:
        return ""
    collapsed = _NON_WORD.sub(" ", text.lower()).strip()
    return f" {collapsed} " if collapsed else ""


@dataclass(frozen=True, slots=True)
class RoutingRule:
    """A routing rule as the matcher needs it."""

    id: int
    site_id: int
    rule_type: str
    pattern: str
    provider_id: int | None = None

    def __post_init__(self) -> None:
        if self.rule_type not in RULE_TYPES:
            raise ValueError(f"Unknown site routing rule type: {self.rule_type!r}")


@dataclass(frozen=True, slots=True)
class _CompiledRule:
    id: int
    needle: str


@dataclass(frozen=True, slots=True)
class _SiteRules:
    excludes: tuple[_CompiledRule, ...]
    classifications: tuple[_CompiledRule, ...]
    titles: tuple[_CompiledRule, ...]


class SiteMatcher:
    """Evaluate a fixed rule set against many jobs of one provider."""

    def __init__(self, rules: Iterable[RoutingRule], *, provider_id: int) -> None:
        grouped: dict[int, dict[str, list[_CompiledRule]]] = {}
        for rule in sorted(rules, key=lambda item: item.id):
            if rule.provider_id is not None and rule.provider_id != provider_id:
                continue
            needle = normalize(rule.pattern)
            if not needle:
                continue
            site_rules = grouped.setdefault(
                rule.site_id, {rule_type: [] for rule_type in RULE_TYPES}
            )
            site_rules[rule.rule_type].append(_CompiledRule(rule.id, needle))

        self._sites: dict[int, _SiteRules] = {
            site_id: _SiteRules(
                excludes=tuple(by_type[EXCLUDE_TITLE]),
                classifications=tuple(by_type[INCLUDE_CLASSIFICATION]),
                titles=tuple(by_type[INCLUDE_TITLE]),
            )
            for site_id, by_type in grouped.items()
        }

    @property
    def site_ids(self) -> Sequence[int]:
        """Sites that have at least one applicable rule."""
        return tuple(self._sites)

    def match(self, *, title: str | None, classification: str | None) -> dict[int, int]:
        """Return ``{site_id: matched_rule_id}`` for every site the job joins."""
        title_text = normalize(title)
        classification_text = normalize(classification)
        assignments: dict[int, int] = {}

        for site_id, rules in self._sites.items():
            if any(rule.needle in title_text for rule in rules.excludes):
                continue
            matched = next(
                (
                    rule.id
                    for rule in rules.classifications
                    if rule.needle == classification_text
                ),
                None,
            )
            if matched is None:
                matched = next(
                    (rule.id for rule in rules.titles if rule.needle in title_text),
                    None,
                )
            if matched is not None:
                assignments[site_id] = matched
        return assignments
