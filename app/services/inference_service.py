"""Pure keyword inference for normalized job metadata."""

from __future__ import annotations

import re
from collections.abc import Sequence

InferenceResult = tuple[str | None, str | None]

_REMOTE_TITLE_RULES: Sequence[tuple[re.Pattern[str], str]] = (
    (re.compile(r"\bremote\b(?!\s+monitoring\b)", re.IGNORECASE), "remote"),
    (re.compile(r"\bhybrid\b", re.IGNORECASE), "hybrid"),
    (re.compile(r"\bonsite\b", re.IGNORECASE), "onsite"),
    (re.compile(r"\bon-site\b", re.IGNORECASE), "onsite"),
    (re.compile(r"\bwork\s+from\s+home\b", re.IGNORECASE), "remote"),
    (re.compile(r"\bin-office\b", re.IGNORECASE), "onsite"),
)

_REMOTE_DESCRIPTION_RULES: Sequence[tuple[re.Pattern[str], str]] = (
    (
        re.compile(
            r"\b(?:fully remote|100% remote|remote position|remote opportunity|"
            r"work from home)\b",
            re.IGNORECASE,
        ),
        "remote",
    ),
    (
        re.compile(
            r"\b(?:hybrid role|hybrid position|hybrid schedule|hybrid work)\b",
            re.IGNORECASE,
        ),
        "hybrid",
    ),
    (
        re.compile(
            r"\b(?:onsite role|on-site role|onsite position|on-site position|"
            r"in-office)\b",
            re.IGNORECASE,
        ),
        "onsite",
    ),
)

_SENIOR_TITLE_PATTERN = re.compile(
    r"(?:\bsenior\b(?!\s+(?:care|living)\b)|\bsr\.(?=\s|$)|\bsr(?=\s))",
    re.IGNORECASE,
)
_JUNIOR_TITLE_PATTERN = re.compile(
    r"(?:\bjunior\b|\bjr\.(?=\s|$)|\bjr(?=\s))",
    re.IGNORECASE,
)
_ENTRY_LEVEL_TITLE_PATTERN = re.compile(r"\bentry(?:\s+|-)level\b", re.IGNORECASE)
_PRINCIPAL_TITLE_PATTERN = re.compile(r"\bprincipal\b", re.IGNORECASE)
_LEAD_TITLE_PATTERN = re.compile(
    r"(?:^lead\s+|^\S+\s+lead(?:\s|$))",
    re.IGNORECASE,
)
_LEAD_FALSE_POSITIVE_PATTERN = re.compile(
    r"(?:^lead\s+exposure(?:\s|$)|^blood\s+lead\s+level(?:\s|$))",
    re.IGNORECASE,
)


def infer_remote_status(title: str, description: str) -> InferenceResult:
    """Infer remote status in {remote, hybrid, onsite}, or return no signal."""
    for pattern, remote_status in _REMOTE_TITLE_RULES:
        if pattern.search(title):
            return remote_status, "inferred"

    for pattern, remote_status in _REMOTE_DESCRIPTION_RULES:
        if pattern.search(description):
            return remote_status, "inferred"

    return None, None


def infer_experience_level(title: str, description: str) -> InferenceResult:
    """Infer level in {entry, mid, senior, lead}, or return no signal.

    ``mid`` is part of the value set but is never inferred here. The description
    is intentionally unused because experience inference is title-only.
    """
    del description

    if _SENIOR_TITLE_PATTERN.search(title):
        return "senior", "inferred"
    if _JUNIOR_TITLE_PATTERN.search(title):
        return "entry", "inferred"
    if _ENTRY_LEVEL_TITLE_PATTERN.search(title):
        return "entry", "inferred"
    if _PRINCIPAL_TITLE_PATTERN.search(title):
        return "lead", "inferred"
    if not _LEAD_FALSE_POSITIVE_PATTERN.search(title) and _LEAD_TITLE_PATTERN.search(
        title
    ):
        return "lead", "inferred"

    return None, None
