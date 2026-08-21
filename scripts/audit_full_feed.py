"""Throwaway read-only audit of the full Jobg8 feed.

Streams every <Job> in the feed ZIP and reports, per risky field, how often
the raw value fails the schema's CURRENT rules -- plus every unmapped element
name the feed contains.

Each field is validated in isolation with a TypeAdapter rather than through
JobFeedRecord, because a single failing field (today: SalaryCurrency, at
100%) would otherwise mask every other field's real failure rate.

Touches no database. Run: python scripts/audit_full_feed.py [zip_path]
"""

import sys
import zipfile
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from lxml import etree
from pydantic import AnyHttpUrl, TypeAdapter, ValidationError

from app.imports.parser import job_element_to_dict
from app.imports.schemas import CurrencyCode, JobFeedRecord, SalaryAmount, SellPriceAmount

DEFAULT_ZIP = r"C:/Users/5A_Traders/Downloads/Jobs.zip"

# Field name -> the exact annotated type that field uses in JobFeedRecord.
TARGETS = {
    "salary_currency": CurrencyCode,
    "sell_price_currency": CurrencyCode,
    "salary_min": SalaryAmount,
    "salary_max": SalaryAmount,
    "sell_price": SellPriceAmount,
    "advertiser_logo_url": AnyHttpUrl,
    "apply_url": AnyHttpUrl,
}

# Cap distinct failing values held in memory, in case failures are unbounded
# (e.g. every URL unique). Counting continues past the cap; only new distinct
# keys stop being added.
MAX_DISTINCT = 5_000
PROGRESS_EVERY = 50_000


def normalize(value):
    """Apply the same blank-is-missing rule the model applies before validation.

    JobFeedRecord strips strings and treats blank elements as missing, so a
    blank tag must count as absent here rather than as a parse failure.
    """
    if isinstance(value, str):
        value = value.strip()
        return value if value else None
    return value


def main() -> None:
    zip_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_ZIP

    fields = {name: f for name, f in JobFeedRecord.model_fields.items()}
    alias_of = {name: (fields[name].alias or name) for name in TARGETS}
    adapters = {name: TypeAdapter(tp) for name, tp in TARGETS.items()}
    known_aliases = {f.alias for f in fields.values() if f.alias}

    tag_present = Counter()      # alias -> records where the tag exists at all
    value_present = Counter()    # alias -> records where it exists and is non-blank
    fail_count = Counter()       # field -> non-blank values that fail validation
    fail_values = {name: Counter() for name in TARGETS}
    fail_reason = {name: Counter() for name in TARGETS}
    unmapped = Counter()         # unknown element name -> records containing it
    total = 0

    with zipfile.ZipFile(zip_path) as zf:
        member = [n for n in zf.namelist() if n.lower().endswith(".xml")][0]
        print(f"feed   : {zip_path}")
        print(f"member : {member}")
        print("streaming... (progress every 50k records)\n", flush=True)

        with zf.open(member) as stream:
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
                if not isinstance(element.tag, str) or etree.QName(element).localname != "Job":
                    continue

                raw = job_element_to_dict(element)
                total += 1
                tag_present.update(raw.keys())

                for tag in raw:
                    if tag not in known_aliases:
                        unmapped[tag] += 1

                for name, adapter in adapters.items():
                    raw_value = raw.get(alias_of[name])
                    value = normalize(raw_value)
                    if value is None:
                        continue
                    value_present[name] += 1
                    try:
                        adapter.validate_python(value)
                    except ValidationError as error:
                        fail_count[name] += 1
                        if len(fail_values[name]) < MAX_DISTINCT or value in fail_values[name]:
                            fail_values[name][value] += 1
                        fail_reason[name][error.errors()[0]["type"]] += 1

                element.clear()
                parent = element.getparent()
                if parent is not None:
                    while element.getprevious() is not None:
                        del parent[0]

                if total % PROGRESS_EVERY == 0:
                    print(f"  ...{total:,} records", flush=True)

    # ------------------------------------------------------------------
    # Report
    # ------------------------------------------------------------------
    print()
    print("=" * 78)
    print(f"FULL FEED AUDIT — {total:,} records")
    print("=" * 78)

    print()
    print(f"{'field':22} {'tag present':>13} {'non-blank':>11} {'failing':>11} {'fail %':>8}")
    print("-" * 78)
    for name in TARGETS:
        alias = alias_of[name]
        present = value_present[name]
        failed = fail_count[name]
        pct = (failed / present * 100) if present else 0.0
        print(
            f"{name:22} {tag_present[alias]:>13,} {present:>11,} "
            f"{failed:>11,} {pct:>7.2f}%"
        )

    print()
    print("=" * 78)
    print("EXAMPLE FAILING RAW VALUES (most frequent first)")
    print("=" * 78)
    for name in TARGETS:
        print()
        print(f"--- {name}  (alias <{alias_of[name]}>)")
        if not fail_count[name]:
            print("    no failures")
            continue
        print(f"    error types: {dict(fail_reason[name])}")
        distinct = len(fail_values[name])
        capped = " (capped)" if distinct >= MAX_DISTINCT else ""
        print(f"    distinct failing values: {distinct:,}{capped}")
        for value, count in fail_values[name].most_common(5):
            shown = value if len(value) <= 90 else value[:87] + "..."
            print(f"      {count:>9,}x  {shown!r}")

    print()
    print("=" * 78)
    print("UNMAPPED FIELD NAMES (present in feed, absent from schema)")
    print("=" * 78)
    if not unmapped:
        print("  none")
    else:
        print(f"  {len(unmapped)} unique unmapped element name(s)\n")
        print(f"  {'element':40} {'records':>12} {'% of feed':>11}")
        print("  " + "-" * 65)
        for tag, count in unmapped.most_common():
            print(f"  {tag:40} {count:>12,} {count/total*100:>10.4f}%")

    print()
    print("=" * 78)
    print("ALL TAG PRESENCE RATES")
    print("=" * 78)
    for tag, count in sorted(tag_present.items()):
        mark = "  " if tag in known_aliases else " *"
        print(f" {mark} {tag:40} {count:>12,} {count/total*100:>8.2f}%")
    print("\n  * = unmapped (not in JobFeedRecord)")


if __name__ == "__main__":
    main()
