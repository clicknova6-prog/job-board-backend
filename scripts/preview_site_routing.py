"""Dry-run site routing rules: counts per rule and sample titles. Writes nothing.

Reads jobs from the database (default) or titles from a CSV export with a
``Title`` column (and optional ``Classification`` column).

Usage:
    python -m scripts.preview_site_routing it-tech
    python -m scripts.preview_site_routing it-tech --csv path/to/All_jobs.csv --samples 10
"""

from __future__ import annotations

import argparse
import csv
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

from sqlalchemy import select

from app.db.models import Job, Site, SiteRoutingRule
from app.db.session import SessionLocal
from app.sites.matcher import RoutingRule, SiteMatcher, normalize


def _rules(
    session, slug: str
) -> tuple[int, list[RoutingRule], dict[int, SiteRoutingRule]]:
    site = session.scalar(select(Site).where(Site.slug == slug))
    if site is None:
        sys.exit(f"Unknown site slug: {slug}")
    rows = list(
        session.scalars(
            select(SiteRoutingRule).where(
                SiteRoutingRule.site_id == site.id, SiteRoutingRule.is_active.is_(True)
            )
        )
    )
    rules = [
        RoutingRule(r.id, r.site_id, r.rule_type, r.pattern, r.provider_id)
        for r in rows
    ]
    return site.id, rules, {r.id: r for r in rows}


def _jobs_from_csv(path: Path):
    csv.field_size_limit(10**9)
    with path.open(encoding="utf-8", errors="replace", newline="") as handle:
        for row in csv.DictReader(handle):
            yield row.get("Title"), row.get("Classification")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("slug")
    parser.add_argument("--csv", type=Path)
    parser.add_argument("--provider-id", type=int, default=0)
    parser.add_argument("--samples", type=int, default=5)
    args = parser.parse_args()

    with SessionLocal() as session:
        site_id, rules, by_id = _rules(session, args.slug)
        if args.csv:
            jobs = list(_jobs_from_csv(args.csv))
        else:
            jobs = list(session.execute(select(Job.title, Job.classification)))

    matcher = SiteMatcher(rules, provider_id=args.provider_id)
    includes_only = SiteMatcher(
        [r for r in rules if r.rule_type != "exclude_title"],
        provider_id=args.provider_id,
    )
    excludes = [r for r in rules if r.rule_type == "exclude_title"]
    hits: dict[int, list[str]] = defaultdict(list)
    excluded: Counter[int] = Counter()
    total = 0
    for title, classification in jobs:
        match = matcher.match(title=title, classification=classification)
        if site_id in match:
            total += 1
            hits[match[site_id]].append(title or "")
        elif site_id in includes_only.match(title=title, classification=classification):
            haystack = normalize(title)
            for rule in excludes:
                if normalize(rule.pattern) in haystack:
                    excluded[rule.id] += 1
                    break

    rng = random.Random(1)
    print(f"{args.slug}: {total:,} of {len(jobs):,} jobs match\n")
    for rule_id, titles in sorted(hits.items(), key=lambda item: -len(item[1])):
        rule = by_id[rule_id]
        print(f"[{len(titles):>6,}] {rule.rule_type}: {rule.pattern}")
        for title in rng.sample(titles, min(args.samples, len(titles))):
            print(f"           - {title}")
    print("\nRemoved by exclude rules (would otherwise have matched):")
    for rule_id, count in excluded.most_common():
        print(f"[{count:>6,}] {by_id[rule_id].pattern}")


if __name__ == "__main__":
    main()
