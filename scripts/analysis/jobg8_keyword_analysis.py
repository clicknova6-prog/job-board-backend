"""Throwaway streaming keyword analysis for a full Jobg8 XML feed archive.

This script is intentionally outside the application. It reads one XML member
directly from a ZIP, keeps only aggregate counters and small sample reservoirs
in memory, and writes a Markdown report.

Run:
    python scripts/analysis/jobg8_keyword_analysis.py path/to/Jobs.zip
"""

from __future__ import annotations

import argparse
import random
import re
import sys
import time
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from lxml import etree

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.imports.parser import job_element_to_dict

DEFAULT_ARCHIVE = Path(r"C:\Users\5A_Traders\Downloads\Jobs.zip")
DEFAULT_REPORT = Path(__file__).with_name("jobg8_keyword_report.md")
PROGRESS_EVERY = 50_000
SAMPLE_COUNT = 5
RANDOM_SEED = 20260824

REMOTE_KEYWORDS = (
    "remote",
    "hybrid",
    "work from home",
    "onsite",
    "on-site",
    "in-office",
)
EXPERIENCE_KEYWORDS = (
    "senior",
    "junior",
    "entry level",
    "entry-level",
    "lead",
    "principal",
    "staff",
    "years of experience",
    "years experience",
)
KEYWORD_GROUPS = {
    "Remote signals": REMOTE_KEYWORDS,
    "Experience signals": EXPERIENCE_KEYWORDS,
}

# This is deliberately a reporting heuristic, not application inference logic.
HEALTHCARE_CLASSIFICATION_PATTERN = re.compile(
    r"health|medical|nursing|pharma|clinical",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class Sample:
    """One description context retained for the report."""

    sender_reference: str
    snippet: str


@dataclass(slots=True)
class KeywordStats:
    """Aggregate counts and a fixed-size description sample reservoir."""

    occurrences: int = 0
    matching_jobs: int = 0
    sampled_jobs_seen: int = 0
    samples: list[Sample] = field(default_factory=list)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Stream a full Jobg8 ZIP and report keyword/classification stats."
    )
    parser.add_argument(
        "archive",
        type=Path,
        nargs="?",
        default=DEFAULT_ARCHIVE,
        help=f"Jobg8 ZIP archive (default: {DEFAULT_ARCHIVE})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_REPORT,
        help=f"Markdown report path (default: {DEFAULT_REPORT})",
    )
    return parser.parse_args()


def _find_xml_member(archive: zipfile.ZipFile) -> zipfile.ZipInfo:
    members = [
        member
        for member in archive.infolist()
        if not member.is_dir() and member.filename.lower().endswith(".xml")
    ]
    if len(members) != 1:
        raise ValueError(f"Expected exactly one XML member, found {len(members)}")
    return members[0]


def _keyword_pattern(keyword: str) -> re.Pattern[str]:
    return re.compile(rf"(?<!\w){re.escape(keyword)}(?!\w)", re.IGNORECASE)


def _description_snippet(
    description: str,
    match: re.Match[str],
    *,
    context_characters: int = 120,
) -> str:
    start = max(0, match.start() - context_characters)
    end = min(len(description), match.end() + context_characters)
    snippet = re.sub(r"\s+", " ", description[start:end]).strip()
    if start > 0:
        snippet = "…" + snippet
    if end < len(description):
        snippet += "…"
    return snippet


def _consider_sample(
    stats: KeywordStats,
    sample: Sample,
    rng: random.Random,
) -> None:
    """Apply reservoir sampling so every matching description is equiprobable."""
    stats.sampled_jobs_seen += 1
    if len(stats.samples) < SAMPLE_COUNT:
        stats.samples.append(sample)
        return
    replacement_index = rng.randrange(stats.sampled_jobs_seen)
    if replacement_index < SAMPLE_COUNT:
        stats.samples[replacement_index] = sample


def analyze_archive(
    archive_path: Path,
) -> tuple[int, Counter[str], dict[str, KeywordStats], float, str]:
    """Stream the archive and return aggregate analysis results."""
    started_at = time.perf_counter()
    classifications: Counter[str] = Counter()
    patterns = {
        keyword: _keyword_pattern(keyword)
        for keywords in KEYWORD_GROUPS.values()
        for keyword in keywords
    }
    stats = {keyword: KeywordStats() for keyword in patterns}
    rng = random.Random(RANDOM_SEED)
    total_jobs = 0

    with zipfile.ZipFile(archive_path) as archive:
        member = _find_xml_member(archive)
        member_name = member.filename
        with archive.open(member) as stream:
            context = etree.iterparse(
                stream,
                events=("end",),
                load_dtd=False,
                no_network=True,
                resolve_entities=False,
                recover=False,
                huge_tree=True,
            )
            for _, element in context:
                if (
                    not isinstance(element.tag, str)
                    or etree.QName(element).localname != "Job"
                ):
                    continue

                raw = job_element_to_dict(element)
                total_jobs += 1
                classification = (raw.get("Classification") or "").strip()
                classifications[classification or "(missing)"] += 1

                sender_reference = (raw.get("SenderReference") or "(missing)").strip()
                description = raw.get("Description") or ""
                position = raw.get("Position") or ""
                for keyword, pattern in patterns.items():
                    description_matches = list(pattern.finditer(description))
                    position_matches = list(pattern.finditer(position))
                    occurrence_count = len(description_matches) + len(position_matches)
                    if not occurrence_count:
                        continue

                    keyword_stats = stats[keyword]
                    keyword_stats.occurrences += occurrence_count
                    keyword_stats.matching_jobs += 1
                    if description_matches:
                        match = rng.choice(description_matches)
                        _consider_sample(
                            keyword_stats,
                            Sample(
                                sender_reference=sender_reference,
                                snippet=_description_snippet(description, match),
                            ),
                            rng,
                        )

                element.clear()
                parent = element.getparent()
                if parent is not None:
                    while element.getprevious() is not None:
                        del parent[0]

                if total_jobs % PROGRESS_EVERY == 0:
                    print(f"Processed {total_jobs:,} jobs", flush=True)

    elapsed_seconds = time.perf_counter() - started_at
    return total_jobs, classifications, stats, elapsed_seconds, member_name


def _markdown_escape(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def write_report(
    output_path: Path,
    *,
    archive_path: Path,
    member_name: str,
    total_jobs: int,
    classifications: Counter[str],
    stats: dict[str, KeywordStats],
    elapsed_seconds: float,
) -> None:
    """Write the completed analysis as Markdown."""
    healthcare_classifications = {
        name: count
        for name, count in classifications.items()
        if HEALTHCARE_CLASSIFICATION_PATTERN.search(name)
    }
    healthcare_jobs = sum(healthcare_classifications.values())
    healthcare_pct = healthcare_jobs / total_jobs * 100 if total_jobs else 0.0

    lines = [
        "# Jobg8 full-feed keyword analysis",
        "",
        f"- Generated: {datetime.now().astimezone().isoformat()}",
        f"- Archive: `{archive_path}`",
        f"- XML member: `{member_name}`",
        f"- Jobs processed: **{total_jobs:,}**",
        f"- Processing time: **{elapsed_seconds:.1f} seconds**",
        f"- Random sample seed: `{RANDOM_SEED}`",
        "",
        (
            "Counts are case-insensitive exact keyword/phrase occurrences across "
            "`<Position>` and `<Description>`. Matching-job counts count each job "
            "once per keyword. Samples are deterministic random `<Description>` "
            "contexts."
        ),
        "",
    ]

    for group_name, keywords in KEYWORD_GROUPS.items():
        lines.extend(
            [
                f"## {group_name}",
                "",
                "| Keyword | Total occurrences | Matching jobs | Description sample pool |",
                "|---|---:|---:|---:|",
            ]
        )
        for keyword in keywords:
            keyword_stats = stats[keyword]
            lines.append(
                f"| `{keyword}` | {keyword_stats.occurrences:,} | "
                f"{keyword_stats.matching_jobs:,} | "
                f"{keyword_stats.sampled_jobs_seen:,} |"
            )
        lines.append("")

        for keyword in keywords:
            lines.extend([f"### `{keyword}` samples", ""])
            keyword_stats = stats[keyword]
            if not keyword_stats.samples:
                lines.extend(["No matching description samples.", ""])
                continue
            for sample in keyword_stats.samples:
                lines.append(
                    f"- `{sample.sender_reference}` — "
                    f"{_markdown_escape(sample.snippet)}"
                )
            lines.append("")

    lines.extend(
        [
            "## Classification distribution",
            "",
            (
                "Healthcare/medical-sounding classifications are identified only "
                "for this report by classification names containing `health`, "
                "`medical`, `nursing`, `pharma`, or `clinical` (case-insensitive)."
            ),
            "",
            (
                f"- Healthcare/medical-sounding jobs: **{healthcare_jobs:,} / "
                f"{total_jobs:,} ({healthcare_pct:.2f}%)**"
            ),
            "- Qualifying classifications: "
            + (
                ", ".join(
                    f"`{name}` ({count:,})"
                    for name, count in sorted(healthcare_classifications.items())
                )
                or "none"
            ),
            "",
            "### Top 20 classifications",
            "",
            "| Rank | Classification | Jobs | Percent |",
            "|---:|---|---:|---:|",
        ]
    )
    for rank, (classification, count) in enumerate(
        classifications.most_common(20),
        start=1,
    ):
        lines.append(
            f"| {rank} | {_markdown_escape(classification)} | {count:,} | "
            f"{count / total_jobs * 100:.2f}% |"
        )
    lines.append("")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    args = _parse_args()
    archive_path = args.archive.resolve()
    if not archive_path.is_file():
        print(f"Error: feed archive not found: {archive_path}", file=sys.stderr)
        return 1

    total_jobs, classifications, stats, elapsed_seconds, member_name = analyze_archive(
        archive_path
    )
    write_report(
        args.output.resolve(),
        archive_path=archive_path,
        member_name=member_name,
        total_jobs=total_jobs,
        classifications=classifications,
        stats=stats,
        elapsed_seconds=elapsed_seconds,
    )
    print(f"Wrote report to {args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
