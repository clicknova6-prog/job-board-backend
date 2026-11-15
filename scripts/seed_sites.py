"""Create or update sites and their routing rules from ``config/sites/*.json``.

Idempotent: sites are matched by ``slug`` and rules by
``(site, provider=None, rule_type, lower(pattern))``. Existing rows are never
deleted, so rules an admin added in the database survive a re-seed. Site
``base_url``/``site_key`` are only set on creation unless ``--update-site``
is passed, so environment-specific values edited in the DB are kept.

Usage:
    python -m scripts.seed_sites                # all config/sites/*.json
    python -m scripts.seed_sites it-tech        # one site
    python -m scripts.seed_sites it-tech --base-url https://example.com --site-key prod-key
    python -m scripts.seed_sites --route        # also re-route all providers
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from app.db.models import Site, SiteRoutingRule
from app.db.session import SessionLocal

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config" / "sites"


def _load(slugs: list[str]) -> list[dict[str, Any]]:
    paths = (
        [CONFIG_DIR / f"{slug}.json" for slug in slugs]
        if slugs
        else sorted(CONFIG_DIR.glob("*.json"))
    )
    return [json.loads(path.read_text(encoding="utf-8")) for path in paths]


def seed(
    definition: dict[str, Any], *, overrides: dict[str, str], update_site: bool
) -> None:
    with SessionLocal() as session:
        site = session.scalar(select(Site).where(Site.slug == definition["slug"]))
        values = {
            "name": definition["name"],
            "site_key": overrides.get("site_key") or definition["site_key"],
            "base_url": (overrides.get("base_url") or definition["base_url"]).rstrip(
                "/"
            ),
        }
        if site is None:
            site = Site(
                slug=definition["slug"], config=definition.get("config", {}), **values
            )
            session.add(site)
            session.flush()
            print(f"created site {site.slug} (id={site.id})")
        elif update_site or overrides:
            for key, value in values.items():
                setattr(site, key, value)
            site.updated_at = func.now()
            print(f"updated site {site.slug}")

        existing = {
            (rule_type, pattern.lower())
            for rule_type, pattern in session.execute(
                select(SiteRoutingRule.rule_type, SiteRoutingRule.pattern).where(
                    SiteRoutingRule.site_id == site.id,
                    SiteRoutingRule.provider_id.is_(None),
                )
            )
        }
        added = 0
        for rule in definition.get("rules", []):
            if (rule["rule_type"], rule["pattern"].lower()) in existing:
                continue
            session.add(
                SiteRoutingRule(
                    site_id=site.id,
                    rule_type=rule["rule_type"],
                    pattern=rule["pattern"],
                    notes=rule.get("notes"),
                )
            )
            added += 1
        session.commit()
        print(f"site {site.slug}: {added} new rule(s), {len(existing)} already present")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("slugs", nargs="*")
    parser.add_argument("--base-url")
    parser.add_argument("--site-key")
    parser.add_argument("--update-site", action="store_true")
    parser.add_argument(
        "--route", action="store_true", help="re-route all providers afterwards"
    )
    args = parser.parse_args()

    overrides = {
        k: v
        for k, v in {"base_url": args.base_url, "site_key": args.site_key}.items()
        if v
    }
    definitions = _load(args.slugs)
    if overrides and len(definitions) != 1:
        parser.error("--base-url/--site-key require exactly one site slug")
    for definition in definitions:
        seed(definition, overrides=overrides, update_site=args.update_site)

    if args.route:
        from app.services.site_routing_service import SiteRoutingService

        for summary in SiteRoutingService().route_all_providers():
            print(summary)


if __name__ == "__main__":
    main()
