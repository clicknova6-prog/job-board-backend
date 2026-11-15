"""Postgres-backed tests for SiteRoutingService maintaining ``job_sites``."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.db.models import Job, JobSite, Provider, Site, SiteRoutingRule
from app.services.site_routing_service import SiteRoutingService

PROVIDER_NAME = "site-routing-integration-provider"
SITE_SLUGS = ("routing-it", "routing-med")
NOW = datetime(2026, 11, 15, tzinfo=UTC)


def _sync_driver_url(url: str) -> str:
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def _purge(session: Session) -> None:
    session.execute(delete(Site).where(Site.slug.in_(SITE_SLUGS)))
    provider_id = session.scalar(
        select(Provider.id).where(Provider.name == PROVIDER_NAME)
    )
    if provider_id is not None:
        session.execute(delete(Job).where(Job.provider_id == provider_id))
        session.execute(delete(Provider).where(Provider.id == provider_id))
    session.commit()


@pytest.fixture()
def session_factory(test_database_url: str) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(_sync_driver_url(test_database_url), poolclass=NullPool)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    with factory() as session:
        _purge(session)
    try:
        yield factory
    finally:
        with factory() as session:
            _purge(session)
        engine.dispose()


def _job(provider_id: int, ref: str, title: str, classification: str | None) -> Job:
    return Job(
        source_name=PROVIDER_NAME,
        provider_id=provider_id,
        source_job_id=ref,
        slug=f"routing-{ref}",
        title=title,
        classification=classification,
        description="Description.",
        apply_url=f"https://example.test/{ref}",
        source_payload={"SenderReference": ref},
        payload_hash=f"hash-{ref}",
        is_active=True,
        first_imported_at=NOW,
        last_imported_at=NOW,
    )


def _seed(factory: sessionmaker[Session]) -> dict[str, int]:
    with factory() as session:
        provider = Provider(name=PROVIDER_NAME, format="xml")
        it_site = Site(
            slug="routing-it",
            name="IT",
            site_key="routing-it-key",
            base_url="https://it.test",
        )
        med_site = Site(
            slug="routing-med",
            name="Med",
            site_key="routing-med-key",
            base_url="https://med.test",
        )
        session.add_all([provider, it_site, med_site])
        session.flush()
        rules = {
            "it_class": SiteRoutingRule(
                site_id=it_site.id,
                rule_type="include_classification",
                pattern="I.T. & Communications",
            ),
            "it_title": SiteRoutingRule(
                site_id=it_site.id,
                rule_type="include_title",
                pattern="software engineer",
            ),
            "it_exclude": SiteRoutingRule(
                site_id=it_site.id, rule_type="exclude_title", pattern="mechanical"
            ),
            "med_class": SiteRoutingRule(
                site_id=med_site.id,
                rule_type="include_classification",
                pattern="Healthcare & Medical",
            ),
        }
        session.add_all(rules.values())
        jobs = {
            "class_it": _job(
                provider.id, "a", "Help Desk Analyst", "I.T. & Communications"
            ),
            "title_it": _job(
                provider.id, "b", "Senior Software Engineer", "Engineering"
            ),
            "excluded": _job(
                provider.id, "c", "Mechanical Software Engineer", "Engineering"
            ),
            "both": _job(
                provider.id, "d", "Clinical Software Engineer", "Healthcare & Medical"
            ),
            "none": _job(provider.id, "e", "Truck Driver", "Transport & Logistics"),
        }
        session.add_all(jobs.values())
        session.commit()
        ids = {name: job.id for name, job in jobs.items()}
        ids |= {name: rule.id for name, rule in rules.items()}
        ids |= {"provider": provider.id, "it": it_site.id, "med": med_site.id}
        return ids


def _assignments(
    factory: sessionmaker[Session], provider_id: int
) -> set[tuple[int, int, int | None]]:
    with factory() as session:
        rows = session.execute(
            select(JobSite.job_id, JobSite.site_id, JobSite.matched_rule_id)
            .join(Job, Job.id == JobSite.job_id)
            .where(Job.provider_id == provider_id)
        )
        return {tuple(row) for row in rows}


def test_routing_assigns_by_rules_and_is_idempotent(
    session_factory: sessionmaker[Session],
) -> None:
    ids = _seed(session_factory)
    service = SiteRoutingService(session_factory=session_factory)

    summary = service.route_provider(ids["provider"])

    assert _assignments(session_factory, ids["provider"]) == {
        (ids["class_it"], ids["it"], ids["it_class"]),
        (ids["title_it"], ids["it"], ids["it_title"]),
        (ids["both"], ids["it"], ids["it_title"]),
        (ids["both"], ids["med"], ids["med_class"]),
    }
    assert summary.assigned == 4
    assert summary.jobs_per_site == {ids["it"]: 3, ids["med"]: 1}

    again = service.route_provider(ids["provider"])
    assert (again.assigned, again.reassigned, again.removed) == (0, 0, 0)


def test_rule_changes_are_applied_on_next_run(
    session_factory: sessionmaker[Session],
) -> None:
    ids = _seed(session_factory)
    service = SiteRoutingService(session_factory=session_factory)
    service.route_provider(ids["provider"])

    with session_factory() as session:
        session.get(SiteRoutingRule, ids["it_title"]).is_active = False
        session.add(
            SiteRoutingRule(
                site_id=ids["it"], rule_type="include_title", pattern="senior"
            )
        )
        session.commit()

    summary = service.route_provider(ids["provider"])

    assignments = _assignments(session_factory, ids["provider"])
    it_jobs = {job_id for job_id, site_id, _ in assignments if site_id == ids["it"]}
    assert it_jobs == {ids["class_it"], ids["title_it"]}
    assert summary.reassigned == 1
    assert summary.removed == 1


def test_deactivated_site_assignments_are_left_untouched(
    session_factory: sessionmaker[Session],
) -> None:
    ids = _seed(session_factory)
    service = SiteRoutingService(session_factory=session_factory)
    service.route_provider(ids["provider"])

    with session_factory() as session:
        session.get(Site, ids["med"]).is_active = False
        session.commit()

    summary = service.route_provider(ids["provider"])

    assert summary.removed == 0
    assert (ids["both"], ids["med"], ids["med_class"]) in _assignments(
        session_factory, ids["provider"]
    )
