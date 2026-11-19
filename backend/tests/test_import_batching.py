"""An import saves its events in batches, not with several commits per event,
and categorizes them from rules it reads once per batch."""

import json

import httpx
import pytest
from sqlalchemy import event as sa_event

from app.database import engine
from app.models.categorization_rule import CategorizationRule
from app.models.event import Event
from app.models.event_category import EventCategory
from app.models.event_provenance import EventProvenance
from app.schemas.extraction import SiteConfiguration
from app.services import extraction_runs
from app.services.categorization import (
    active_rules_cached,
    assign_category,
    reload_cached_rules,
)
from app.services.extraction_runs import preview_extraction, run_extraction
from app.services.fingerprints import update_fingerprint_and_duplicates
from app.services.website_configuration import approve_configuration
from tests.extraction_helpers import patched_http_fetch


def _listing(count: int, *, suffix: str = "") -> str:
    events = [
        {
            "@context": "https://schema.org",
            "@type": "Event",
            "name": f"Concert {i} in the park{suffix}",
            "startDate": f"2027-03-{1 + i % 28:02d}T19:00:00-05:00",
            "url": f"https://example.com/events/e{i}",
            "location": {"@type": "Place", "name": f"Venue {i % 7}"},
        }
        for i in range(count)
    ]
    return (
        '<html><head><script type="application/ld+json">'
        + json.dumps(events)
        + "</script></head><body></body></html>"
    )


def _handler(body: str):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body, headers={"content-type": "text/html"})

    return handler


@pytest.fixture
def approved_website(db_session, make_city, make_website, make_user):
    city = make_city()
    website = make_website(city, name="Batch Source", base_url="https://example.com")
    admin = make_user(email="batch-approver@example.com")
    website.configuration = SiteConfiguration(
        pattern_name="json_ld_event", listing_url="https://example.com/events"
    ).model_dump(mode="json")
    db_session.commit()
    return website, admin


async def _approve(db_session, approved_website):
    """approve_configuration needs a successful preview first, as in the admin UI."""
    website, admin = approved_website
    with patched_http_fetch(_handler(_listing(2))):
        await preview_extraction(db_session, website)
    approve_configuration(db_session, website, approved_by_user_id=admin.id)
    return website


class _CommitCounter:
    def __init__(self) -> None:
        self.count = 0

    def __call__(self, connection) -> None:
        self.count += 1

    def __enter__(self):
        sa_event.listen(engine, "commit", self)
        return self

    def __exit__(self, *exc) -> None:
        sa_event.remove(engine, "commit", self)


@pytest.mark.asyncio
async def test_import_commits_once_per_batch_not_per_event(db_session, approved_website):
    website = await _approve(db_session, approved_website)
    count = 120

    with patched_http_fetch(_handler(_listing(count))), _CommitCounter() as commits:
        result = await run_extraction(db_session, website, triggered_by_user_id=None)

    assert result.events_inserted == count
    assert db_session.query(Event).filter_by(website_id=website.id).count() == count
    assert db_session.query(EventProvenance).filter_by(website_id=website.id).count() == count
    batches = -(-count // extraction_runs.IMPORT_COMMIT_BATCH_SIZE)
    # One per batch, plus the run record, the final save and the source's health.
    assert commits.count <= batches + 5
    assert commits.count < count  # it used to be three per event

    # A re-import updates every event in place, also in batches.
    renamed = _handler(_listing(count, suffix=" live"))
    with patched_http_fetch(renamed), _CommitCounter() as commits:
        result = await run_extraction(db_session, website, triggered_by_user_id=None)

    assert result.events_updated == count
    assert result.events_inserted == 0
    assert commits.count <= batches + 5
    titles = {t for (t,) in db_session.query(Event.title).filter_by(website_id=website.id)}
    assert titles == {f"Concert {i} in the park live" for i in range(count)}
    assert db_session.query(EventProvenance).filter_by(website_id=website.id).count() == 2 * count


@pytest.mark.asyncio
async def test_import_still_flags_duplicates_and_categorizes(
    db_session, approved_website, make_event
):
    from datetime import date

    from app.services.categorization import seed_rules

    seed_rules(db_session)
    db_session.commit()
    website = await _approve(db_session, approved_website)
    other = make_event(
        website.city,
        title="Concert 3 in the park",
        canonical_url="https://another-source.example.org/concert-3",
        start_date=date(2027, 3, 4),
    )
    update_fingerprint_and_duplicates(db_session, other)

    with patched_http_fetch(_handler(_listing(60))):
        await run_extraction(db_session, website, triggered_by_user_id=None)

    db_session.refresh(other)
    imported = db_session.query(Event).filter_by(website_id=website.id).all()
    twin = next(e for e in imported if e.title == "Concert 3 in the park")
    assert twin.duplicate_status == "possible_duplicate"
    assert other.duplicate_status == "possible_duplicate"
    # Every imported event got a category from the rules (or the Other fallback).
    assert all(e.category_id is not None for e in imported)


def test_cached_rules_are_reloaded_after_a_rule_changes(db_session, make_city, make_event):
    category = db_session.query(EventCategory).filter_by(slug="music").first() or (
        db_session.query(EventCategory).first()
    )
    rule = CategorizationRule(
        name="Jazz",
        rule_type="keyword",
        pattern="jazz",
        category_id=category.id,
        is_active=True,
        priority=10,
    )
    db_session.add(rule)
    db_session.commit()
    event = make_event(make_city(), title="Jazz night")

    with active_rules_cached(db_session):
        assert assign_category(db_session, event).rule_id == rule.id
        db_session.delete(rule)
        db_session.commit()
        reload_cached_rules(db_session)
        # The deleted rule is gone from the cache, not read from a stale copy.
        result = assign_category(db_session, event)
        assert result is None or result.rule_id != rule.id
    assert "categorization_active_rules" not in db_session.info
