"""Freshness timestamps: when a town's listings were last checked, and when an
event's visitor-facing details last changed (app.services.event_freshness)."""

import dataclasses
import json
import re
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest

from app.core.permissions import ADMINISTRATOR
from app.models.event import Event
from app.models.event_provenance import EventProvenance
from app.models.extraction_run import ExtractionRun
from app.schemas.extraction import SiteConfiguration
from app.services import extraction_runs
from app.services.event_freshness import (
    local_timestamp,
    town_listings_checked,
    visitor_snapshot,
)
from app.services.extraction_runs import preview_extraction, run_extraction
from app.services.website_configuration import approve_configuration
from tests.extraction_helpers import blocked_handler, patched_http_fetch

CONFIG = SiteConfiguration(pattern_name="json_ld_event", listing_url="https://example.com/events")
APPROVED = {"configuration": CONFIG.model_dump(mode="json")}
FUTURE = date.today() + timedelta(days=30)


def _event_json(**overrides) -> dict:
    item = {
        "@context": "https://schema.org",
        "@type": "Event",
        "name": "Jazz Night at the Park",
        "startDate": f"{FUTURE.isoformat()}T19:00:00",
        "endDate": f"{FUTURE.isoformat()}T22:00:00",
        "description": "An evening of live jazz.",
        "url": "https://example.com/events/jazz-night",
        "image": "https://example.com/images/jazz.jpg",
        "location": {
            "@type": "Place",
            "name": "Central Park Bandshell",
            "address": {"@type": "PostalAddress", "streetAddress": "123 Park Ave"},
        },
    }
    item.update(overrides)
    return item


class Listing:
    """A source page whose events a test can edit between checks."""

    def __init__(self, *events: dict):
        self.events = list(events) or [_event_json()]

    def handler(self, request: httpx.Request) -> httpx.Response:
        scripts = "".join(
            f'<script type="application/ld+json">{json.dumps(e)}</script>' for e in self.events
        )
        body = f"<!DOCTYPE html><html><head><title>Events</title>{scripts}</head></html>"
        return httpx.Response(200, text=body, headers={"content-type": "text/html"})


async def _check(db_session, website, listing: Listing):
    with patched_http_fetch(listing.handler):
        return await run_extraction(db_session, website, triggered_by_user_id=None)


@pytest.fixture
async def source(db_session, make_city, make_website, make_user):
    city = make_city(name="Bloomington", slug="bloomington", timezone="America/New_York")
    website = make_website(city, name="Source A", base_url="https://example.com")
    website.configuration = CONFIG.model_dump(mode="json")
    db_session.commit()
    admin = make_user(email="freshness-approver@example.com")
    with patched_http_fetch(Listing().handler):
        await preview_extraction(db_session, website)
    approve_configuration(db_session, website, approved_by_user_id=admin.id)
    return website


@pytest.fixture
def alerts_fired(monkeypatch):
    fired = []
    monkeypatch.setattr(
        extraction_runs,
        "_fire_alerts",
        lambda db, event, *, new, cancelled: fired.append((event.id, new, cancelled)),
    )
    return fired


# --- Change detection during checks -------------------------------------------


@pytest.mark.asyncio
async def test_new_event_starts_its_update_time(db_session, source, alerts_fired):
    result = await _check(db_session, source, Listing())
    event = db_session.query(Event).one()
    assert result.events_inserted == 1
    assert event.content_updated_at is not None
    assert alerts_fired == [(event.id, True, False)]


@pytest.mark.asyncio
async def test_check_without_changes_keeps_update_time(db_session, source, alerts_fired):
    listing = Listing()
    await _check(db_session, source, listing)
    event = db_session.query(Event).one()
    first_change, first_seen = event.content_updated_at, event.scraped_at
    alerts_fired.clear()

    result = await _check(db_session, source, listing)

    db_session.refresh(event)
    assert event.content_updated_at == first_change
    assert event.scraped_at > first_seen  # the check itself is still recorded
    assert result.events_updated == 1 and result.events_changed == 0
    assert alerts_fired == []  # nobody is told "details changed" when none did
    run = db_session.get(ExtractionRun, result.run_id)
    assert (run.events_changed, run.events_cancelled, run.events_missing) == (0, 0, 0)
    # Provenance history is still appended for every check.
    assert db_session.query(EventProvenance).filter_by(event_id=event.id).count() == 2


@pytest.mark.asyncio
async def test_many_unchanged_checks_never_move_any_event(db_session, source, alerts_fired):
    listing = Listing(
        _event_json(),
        _event_json(name="Poetry Slam", url="https://example.com/events/slam"),
        _event_json(name="Film Club", url="https://example.com/events/film"),
    )
    await _check(db_session, source, listing)
    before = {e.id: e.content_updated_at for e in db_session.query(Event)}
    for _ in range(3):
        result = await _check(db_session, source, listing)
        assert result.events_changed == 0
    db_session.expire_all()
    assert {e.id: e.content_updated_at for e in db_session.query(Event)} == before


@pytest.mark.parametrize(
    "change",
    [
        {"name": "Jazz Night at the Park (moved indoors)"},
        {"description": "An evening of live jazz and blues."},
        {"startDate": f"{FUTURE.isoformat()}T20:00:00"},
        {"location": {"@type": "Place", "name": "Old Library"}},
        {"image": "https://example.com/images/jazz-2.jpg"},
    ],
)
@pytest.mark.asyncio
async def test_changed_details_move_update_time(db_session, source, alerts_fired, change):
    listing = Listing()
    await _check(db_session, source, listing)
    event = db_session.query(Event).one()
    first_change = event.content_updated_at
    alerts_fired.clear()

    listing.events = [_event_json(**change)]
    result = await _check(db_session, source, listing)

    db_session.refresh(event)
    assert event.content_updated_at > first_change
    assert result.events_changed == 1
    assert alerts_fired == [(event.id, False, False)]


@pytest.mark.asyncio
async def test_source_link_change_alone_is_not_a_content_update(db_session, source):
    await _check(db_session, source, Listing(_event_json(identifier="jazz-1")))
    event = db_session.query(Event).one()
    first_change = event.content_updated_at

    await _check(
        db_session,
        source,
        Listing(_event_json(identifier="jazz-1", url="https://example.com/events/jazz?utm=x")),
    )

    db_session.refresh(event)
    assert event.content_updated_at == first_change


@pytest.mark.asyncio
async def test_cancelled_at_source_is_counted_and_moves_update_time(
    db_session, source, alerts_fired, monkeypatch
):
    listing = Listing()
    await _check(db_session, source, listing)
    event = db_session.query(Event).one()
    first_change = event.content_updated_at
    alerts_fired.clear()

    real = extraction_runs._execute_pipeline

    async def cancelled_pipeline(*args, **kwargs):
        outcome = await real(*args, **kwargs)
        return dataclasses.replace(
            outcome,
            outcomes=[(dataclasses.replace(c, is_cancelled=True), r) for c, r in outcome.outcomes],
        )

    monkeypatch.setattr(extraction_runs, "_execute_pipeline", cancelled_pipeline)
    result = await _check(db_session, source, listing)

    db_session.refresh(event)
    assert result.events_cancelled == 1 and result.events_changed == 1
    assert event.is_cancelled and not event.is_active
    assert event.content_updated_at > first_change
    assert alerts_fired == [(event.id, False, True)]


@pytest.mark.asyncio
async def test_event_no_longer_listed_is_counted_but_kept(db_session, source):
    listing = Listing(
        _event_json(), _event_json(name="Poetry Slam", url="https://example.com/events/slam")
    )
    await _check(db_session, source, listing)
    listing.events = listing.events[:1]

    result = await _check(db_session, source, listing)

    assert result.events_missing == 1
    slam = db_session.query(Event).filter_by(title="Poetry Slam").one()
    assert slam.is_active  # absence from one listing never hides an event


@pytest.mark.asyncio
async def test_failed_check_changes_no_event(db_session, source):
    await _check(db_session, source, Listing())
    event = db_session.query(Event).one()
    first_change, first_seen = event.content_updated_at, event.scraped_at

    with patched_http_fetch(blocked_handler()):
        result = await run_extraction(db_session, source, triggered_by_user_id=None)

    assert result.status == "blocked"
    db_session.refresh(event)
    assert (event.content_updated_at, event.scraped_at) == (first_change, first_seen)


@pytest.mark.asyncio
async def test_scraper_change_under_an_admin_correction_is_not_an_update(
    db_session, source
):
    listing = Listing()
    await _check(db_session, source, listing)
    event = db_session.query(Event).one()
    event.corrected_venue = "Corrected Hall"
    db_session.commit()
    first_change = event.content_updated_at

    listing.events = [_event_json(location={"@type": "Place", "name": "Another Venue"})]
    result = await _check(db_session, source, listing)

    db_session.refresh(event)
    assert event.public_venue == "Corrected Hall"  # the correction still wins
    assert event.venue == "Another Venue"  # the source value is kept underneath
    assert event.content_updated_at == first_change
    assert result.events_changed == 0


# --- Admin corrections -----------------------------------------------------------


def _admin(client, make_user, login):
    make_user(email="fresh-admin@example.com", role_name=ADMINISTRATOR)
    login("fresh-admin@example.com")
    return client.cookies.get("csrf_token")


def test_location_correction_moves_update_time_once(
    client, make_user, login, make_city, make_event, db_session
):
    csrf = _admin(client, make_user, login)
    old = datetime(2026, 1, 1, tzinfo=UTC)
    event = make_event(make_city(), venue="Source Venue", content_updated_at=old)
    form = {"venue": "Corrected Venue", "address": "", "latitude": "", "longitude": ""}

    client.post(f"/admin/events/{event.id}/location-correction", data={**form, "csrf_token": csrf})
    db_session.refresh(event)
    corrected_at = event.content_updated_at
    assert corrected_at.replace(tzinfo=UTC) > old

    # Saving the same correction again changes nothing a visitor sees.
    client.post(f"/admin/events/{event.id}/location-correction", data={**form, "csrf_token": csrf})
    db_session.refresh(event)
    assert event.content_updated_at == corrected_at

    client.post(f"/admin/events/{event.id}/location-correction/clear", data={"csrf_token": csrf})
    db_session.refresh(event)
    assert event.public_venue == "Source Venue"
    assert event.content_updated_at > corrected_at


def test_category_override_moves_update_time(
    client, make_user, login, make_city, make_event, make_category, db_session
):
    csrf = _admin(client, make_user, login)
    old = datetime(2026, 1, 1, tzinfo=UTC)
    music = make_category(name="Fresh Music", slug="fresh-music")
    talks = make_category(name="Fresh Talks", slug="fresh-talks")
    event = make_event(make_city(), category=music, content_updated_at=old)

    client.post(
        f"/admin/events/{event.id}/category-override",
        data={"category_id": music.id, "csrf_token": csrf},
    )
    db_session.refresh(event)
    assert event.content_updated_at.replace(tzinfo=UTC) == old  # same category shown

    client.post(
        f"/admin/events/{event.id}/category-override",
        data={"category_id": talks.id, "csrf_token": csrf},
    )
    db_session.refresh(event)
    assert event.content_updated_at.replace(tzinfo=UTC) > old


def test_snapshot_ignores_lifecycle_and_scrape_metadata(make_city, make_event):
    event = make_event(make_city())
    before = visitor_snapshot(event)
    event.scraped_at = datetime.now(UTC)
    event.review_status = "needs_review"
    event.geocoded_latitude = 1.0
    event.fingerprint = "x" * 64
    assert visitor_snapshot(event) == before


# --- Town-level freshness from run records -----------------------------------------


def _source(make_website, city, name, **values):
    return make_website(
        city, name=name, base_url=f"https://{name}.example.com", is_active=True,
        approved_pattern=APPROVED, **values,
    )


def _run(db_session, website, finished, status="success", run_type="manual"):
    run = ExtractionRun(
        website_id=website.id, run_type=run_type, status=status, source_url=website.base_url,
        started_at=finished - timedelta(minutes=1), completed_at=finished,
    )
    db_session.add(run)
    db_session.commit()
    return run


T0 = datetime(2026, 11, 20, 1, 45, tzinfo=UTC)  # 8:45 PM EST on November 19


@pytest.fixture
def town(make_city):
    return make_city(name="Bloomington", slug="bloomington", timezone="America/New_York")


def test_no_sources_or_no_successful_check_means_no_timestamp(
    db_session, make_website, town
):
    assert town_listings_checked(db_session, town.id) is None
    site = _source(make_website, town, "a")
    _run(db_session, site, T0, status="failed")
    assert town_listings_checked(db_session, town.id) is None


def test_single_source_uses_its_latest_successful_check(db_session, make_website, town):
    site = _source(make_website, town, "a")
    _run(db_session, site, T0 - timedelta(hours=6))
    _run(db_session, site, T0)
    freshness = town_listings_checked(db_session, town.id, now=T0 + timedelta(hours=1))
    assert freshness.checked_at == T0
    assert (freshness.sources, freshness.failing) == (1, 0)


def test_multiple_sources_report_when_all_were_checked(db_session, make_website, town):
    hourly, daily = _source(make_website, town, "hourly"), _source(make_website, town, "daily")
    _run(db_session, hourly, T0)
    _run(db_session, daily, T0 - timedelta(hours=20))
    freshness = town_listings_checked(db_session, town.id, now=T0)
    assert freshness.checked_at == T0 - timedelta(hours=20)
    assert freshness.sources == 2


def test_failed_check_keeps_last_success_and_is_flagged(db_session, make_website, town):
    good, broken = _source(make_website, town, "good"), _source(make_website, town, "broken")
    _run(db_session, broken, T0 - timedelta(days=1))
    _run(db_session, broken, T0, status="failed")
    _run(db_session, good, T0)
    _run(db_session, good, T0 + timedelta(minutes=5), status="blocked")
    _run(db_session, good, T0 + timedelta(minutes=10))
    freshness = town_listings_checked(db_session, town.id, now=T0 + timedelta(hours=1))
    # The broken source has not been checked since yesterday, so neither has the town.
    assert freshness.checked_at == T0 - timedelta(days=1)
    assert freshness.failing == 1  # "good" recovered after its blocked run


def test_partial_run_counts_as_a_completed_check(db_session, make_website, town):
    site = _source(make_website, town, "a")
    _run(db_session, site, T0, status="partial")
    assert town_listings_checked(db_session, town.id, now=T0).checked_at == T0


def test_preview_and_detection_runs_are_not_checks(db_session, make_website, town):
    site = _source(make_website, town, "a")
    _run(db_session, site, T0 - timedelta(days=2))
    _run(db_session, site, T0, run_type="preview")
    _run(db_session, site, T0, run_type="detection")
    assert town_listings_checked(db_session, town.id, now=T0).checked_at == T0 - timedelta(days=2)


def test_disabled_archived_unapproved_and_new_sources_are_left_out(
    db_session, make_website, town, make_city
):
    live = _source(make_website, town, "live")
    _run(db_session, live, T0)
    old = T0 - timedelta(days=30)
    disabled = make_website(town, name="off", is_active=False, approved_pattern=APPROVED)
    archived = _source(make_website, town, "archived", archived=True)
    unapproved = make_website(town, name="draft", is_active=True)
    for site in (disabled, archived, unapproved):
        _run(db_session, site, old)
    _source(make_website, town, "just-onboarded")  # no run yet
    other_town = make_city(name="Elsewhere", slug="elsewhere")
    _run(db_session, _source(make_website, other_town, "elsewhere"), old)

    freshness = town_listings_checked(db_session, town.id, now=T0)
    assert freshness.checked_at == T0
    assert freshness.sources == 1
    assert freshness.pending == 1  # the newly approved source is not ignored


def test_a_check_is_never_shown_in_the_future(db_session, make_website, town):
    site = _source(make_website, town, "a")
    _run(db_session, site, T0 + timedelta(minutes=3))  # scheduler clock ahead
    assert town_listings_checked(db_session, town.id, now=T0).checked_at == T0


# --- Formatting ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("moment", "zone", "expected"),
    [
        (T0, "America/New_York", "November 19, 2026 at 8:45 PM EST"),
        (datetime(2026, 7, 4, 20, 32, tzinfo=UTC), "America/New_York",
         "July 4, 2026 at 4:32 PM EDT"),
        (T0, "America/Chicago", "November 19, 2026 at 7:45 PM CST"),
        (T0, "America/Indiana/Indianapolis", "November 19, 2026 at 8:45 PM EST"),
        # A bare "EST" setting still follows daylight saving time.
        (datetime(2026, 7, 4, 20, 32, tzinfo=UTC), "EST", "July 4, 2026 at 4:32 PM EDT"),
        # Spring forward: 1:59 AM EST is followed by 3:00 AM EDT.
        (datetime(2026, 3, 8, 6, 59, tzinfo=UTC), "America/New_York",
         "March 8, 2026 at 1:59 AM EST"),
        (datetime(2026, 3, 8, 7, 0, tzinfo=UTC), "America/New_York",
         "March 8, 2026 at 3:00 AM EDT"),
        # Fall back: 1:30 AM happens twice, once in EDT and once in EST.
        (datetime(2026, 11, 1, 5, 30, tzinfo=UTC), "America/New_York",
         "November 1, 2026 at 1:30 AM EDT"),
        (datetime(2026, 11, 1, 6, 30, tzinfo=UTC), "America/New_York",
         "November 1, 2026 at 1:30 AM EST"),
        # A town still on the UTC default says so rather than guessing.
        (T0, "UTC", "November 20, 2026 at 1:45 AM UTC"),
        # SQLite hands back naive datetimes; they are UTC.
        (T0.replace(tzinfo=None), "America/New_York", "November 19, 2026 at 8:45 PM EST"),
    ],
)
def test_local_timestamp(moment, zone, expected):
    assert local_timestamp(moment, zone) == expected


# --- Public pages ----------------------------------------------------------------------


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


@pytest.mark.parametrize("path", ["", "/today", "/this-weekend"])
def test_town_pages_show_when_listings_were_checked(
    client, db_session, make_website, town, path
):
    _run(db_session, _source(make_website, town, "a"), T0)
    page = client.get(f"/city/bloomington{path}")
    assert page.status_code == 200
    assert "Listings checked November 19, 2026 at 8:45 PM EST" in _text(page.text)
    assert '<time datetime="2026-11-20T01:45:00+00:00">' in page.text
    assert "could not be checked" not in page.text


def test_town_page_mentions_a_source_that_failed(client, db_session, make_website, town):
    site = _source(make_website, town, "a")
    _run(db_session, site, T0)
    _run(db_session, site, T0 + timedelta(hours=1), status="failed")
    text = _text(client.get("/city/bloomington").text)
    assert "Listings checked November 19, 2026 at 8:45 PM EST" in text
    # A town's only source is named too, so it never reads as current.
    assert (
        ". a could not be checked on the latest try, so this is the time of its "
        "last successful check" in text
    )
    assert "Every other source" not in text


def test_town_page_without_checks_shows_nothing_and_views_change_nothing(
    client, db_session, make_website, town
):
    assert "Listings checked" not in client.get("/city/bloomington").text
    site = _source(make_website, town, "a")
    _run(db_session, site, T0)
    first = client.get("/city/bloomington").text
    second = client.get("/city/bloomington").text
    assert "8:45 PM EST" in first and "8:45 PM EST" in second
    assert db_session.query(ExtractionRun).count() == 1


def test_event_page_shows_when_details_last_changed(
    client, db_session, make_website, make_event, town
):
    site = _source(make_website, town, "a")
    changed = datetime(2026, 11, 19, 21, 32, tzinfo=UTC)
    event = make_event(
        town, website=site, start_date=FUTURE, content_updated_at=changed,
        scraped_at=T0,
    )
    text = _text(client.get(f"/events/{event.id}").text)
    assert "Event information last updated November 19, 2026 at 4:32 PM EST" in text


def test_event_page_with_unknown_change_time_says_nothing(
    client, db_session, make_website, make_event, town
):
    site = _source(make_website, town, "a")
    event = make_event(town, website=site, start_date=FUTURE, scraped_at=T0)
    page = client.get(f"/events/{event.id}")
    assert page.status_code == 200
    assert "last updated" not in page.text


def test_new_source_without_a_first_check_is_counted_not_ignored(
    client, db_session, make_website, town
):
    established = _source(make_website, town, "established")
    _run(db_session, established, T0)
    new = _source(make_website, town, "new")
    _run(db_session, new, T0 + timedelta(minutes=5), status="failed")  # first import failed

    freshness = town_listings_checked(db_session, town.id, now=T0 + timedelta(hours=1))
    assert freshness.checked_at == T0  # dated by the established source only
    assert (freshness.sources, freshness.failing, freshness.pending) == (1, 0, 1)

    text = _text(client.get("/city/bloomington").text)
    assert "Listings checked November 19, 2026 at 8:45 PM EST" in text
    assert "1 source has not completed a first check yet" in text
    assert "could not be checked" not in text


def test_town_page_lists_every_note(client, db_session, make_website, town):
    broken = _source(make_website, town, "broken")
    _run(db_session, broken, T0)
    _run(db_session, broken, T0 + timedelta(hours=1), status="failed")
    _source(make_website, town, "new-1")
    _source(make_website, town, "new-2")
    text = _text(client.get("/city/bloomington").text)
    assert "could not be checked on the latest try" in text
    assert "2 sources have not completed a first check yet" in text
