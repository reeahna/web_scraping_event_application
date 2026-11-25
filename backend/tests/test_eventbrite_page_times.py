"""Start times for Eventbrite listings that give only a date.

Eventbrite's city pages ("/d/in--bloomington/all-events/") publish each event
as JSON-LD with a bare `startDate` ("2026-11-24"), while the event's own page
states the full start time. The page lookup reads those pages, but only for
events the run will keep (out-of-town listings are skipped before any fetch),
and never guesses a time a page doesn't state.
"""

import json
from datetime import date, time

import httpx
import pytest

from app.config import get_settings
from app.extraction.event_page_fallback import (
    fill_from_event_pages,
    page_states_time,
    time_gap,
)
from app.extraction.normalize import normalize_candidate
from app.extraction.types import EventCandidate
from app.models.event import Event
from app.models.extraction_run import ExtractionRun
from app.schemas.extraction import FetchConfig, SiteConfiguration
from app.services.extraction_runs import preview_extraction, run_extraction
from app.services.website_configuration import approve_configuration
from tests.extraction_helpers import make_response, patched_http_fetch

INDY = "America/Indiana/Indianapolis"
LISTING_URL = "https://www.eventbrite.com/d/in--bloomington/all-events/"
HUNT = "https://www.eventbrite.com/e/bloomington-indiana-scavenger-hunt-tickets-1996270033408"
FOODIES = (
    "https://www.eventbrite.com/e/"
    "foodies-new-friends-bloomington-giving-back-edition-tickets-1987538302566"
)
INDY_BREWFEST = "https://www.eventbrite.com/e/indy-brewfest-tickets-1990000000001"
DESCRIPTION = "Solve clues, tackle wacky tasks, and explore your city's uncovered gems."
CONFIG = SiteConfiguration(pattern_name="json_ld_event", listing_url=LISTING_URL)


@pytest.fixture(autouse=True)
def _home_town_filter_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "home_town_filter_enabled", True)


def _detail_page(url: str, start: object, end: object = None) -> str:
    event = {"@context": "https://schema.org", "@type": "Event", "name": "E", "url": url}
    if start is not None:
        event["startDate"] = start
    if end is not None:
        event["endDate"] = end
    return (
        f'<html><head><script type="application/ld+json">{json.dumps(event)}</script>'
        "</head><body></body></html>"
    )


def _listing(url: str, start: str = "2026-11-24", **raw) -> EventCandidate:
    """A candidate as Eventbrite's listing gives it: date only, with a
    description and a venue (so nothing else would send it to its page)."""
    base = {
        "title": "Bloomington Indiana Scavenger Hunt",
        "canonical_url": url,
        "start_datetime": start,
        "description": DESCRIPTION,
        "venue": "Hopscotch Coffee & Kitchen",
        "address": "235 West Dodds Street, Bloomington, IN, 47403",
    }
    return EventCandidate(
        raw={**base, **raw}, title=None, canonical_url=None, description=None,
        start_date=None, start_time=None, end_date=None, end_time=None, timezone=None,
        venue=None, address=None, image_url=None, latitude=None, longitude=None,
        source_category=None, external_source_id=None, field_source_paths={},
        transformation_history=(), source_page=LISTING_URL, extraction_pattern="json_ld_event",
        warnings=(), raw_record_hash=url,
    )


class _PageFetch:
    def __init__(self, pages: dict[str, str]):
        self.pages = pages
        self.fetched: list[str] = []

    async def fetch(self, request, config):
        self.fetched.append(request.url)
        body = self.pages.get(request.url)
        return make_response(body or "", final_url=request.url, status_code=200 if body else 404)


def _local(candidate: EventCandidate):
    event = normalize_candidate(candidate, CONFIG.model_copy(update={"timezone": INDY}))
    return event.start_date, event.start_time, event.end_time


# --- which pages are read ------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (HUNT, True),
        ("https://eventbrite.com/e/x-1", True),
        ("https://www.eventbrite.co.uk/e/x-1", True),
        ("https://www.eventbrite.com.au/e/x-1", True),
        ("https://example.com/e/x-1", False),
        ("https://eventbrite.example.com/e/x-1", False),
        ("https://www.noteventbrite.com/e/x-1", False),
    ],
)
def test_only_eventbrite_pages_are_known_to_state_times(url, expected):
    assert page_states_time(url) is expected


@pytest.mark.asyncio
async def test_date_only_eventbrite_listing_gets_its_time_from_its_page():
    fetch = _PageFetch(
        {HUNT: _detail_page(HUNT, "2026-11-24T10:00:00-05:00", "2026-11-24T12:00:00-05:00")}
    )
    listing = _listing(HUNT)
    assert time_gap(listing.raw) == "date_only"
    (filled,), warnings = await fill_from_event_pages(
        [listing], fetch, FetchConfig(), timezone=INDY
    )
    assert fetch.fetched == [HUNT]
    assert _local(filled) == (date(2026, 11, 24), time(10), time(12))
    assert filled.field_source_paths["start_datetime"] == "detail:jsonld.startDate"
    # Everything else is as the listing gave it.
    for field in ("title", "canonical_url", "description", "venue", "address"):
        assert filled.raw[field] == listing.raw[field]
    assert "time_lookup:1_filled_of_1_pages" in warnings


@pytest.mark.asyncio
async def test_date_only_listing_elsewhere_still_isnt_fetched_for_a_time():
    other = "https://example.com/events/talk"
    fetch = _PageFetch({other: _detail_page(other, "2026-11-24T10:00:00-05:00")})
    await fill_from_event_pages([_listing(other)], fetch, FetchConfig(), timezone=INDY)
    assert fetch.fetched == []


@pytest.mark.asyncio
async def test_events_that_already_have_a_time_are_not_fetched():
    fetch = _PageFetch({
        HUNT: _detail_page(HUNT, "2026-11-24T10:00:00-05:00"),
        FOODIES: _detail_page(FOODIES, "2026-11-24T18:00:00-05:00"),
    })
    timed_in_listing = _listing(HUNT, "2026-11-24T19:00:00-05:00")
    stored_with_time = _listing(FOODIES)
    result, _ = await fill_from_event_pages(
        [timed_in_listing, stored_with_time], fetch, FetchConfig(),
        known_timed_urls=frozenset({FOODIES}), timezone=INDY,
    )
    assert fetch.fetched == []
    assert result[0].raw["start_datetime"] == "2026-11-24T19:00:00-05:00"
    assert result[1].raw["start_datetime"] == "2026-11-24"


@pytest.mark.parametrize(
    "page",
    [
        _detail_page(HUNT, None),  # no startDate at all
        _detail_page(HUNT, "2026-11-24"),  # a date, no time
        _detail_page(HUNT, "2026-11-24T00:00:00"),  # a bare midnight placeholder
        _detail_page(HUNT, "sometime in the afternoon"),
        _detail_page(HUNT, "2026-11-24T25:61:00-05:00"),  # not a real time
        _detail_page(HUNT, "2026-11-24T18:00:00-05:00", "2026-11-24T17:00:00-05:00"),  # ends first
        "<html><head><script type='application/ld+json'>{not json</script></head></html>",
        "",  # page not found
    ],
)
@pytest.mark.asyncio
async def test_page_without_a_reliable_time_leaves_time_tbd(page):
    fetch = _PageFetch({HUNT: page} if page else {})
    listing = _listing(HUNT)
    (kept,), warnings = await fill_from_event_pages([listing], fetch, FetchConfig(), timezone=INDY)
    assert fetch.fetched == [HUNT]
    assert kept.raw == listing.raw
    assert _local(kept) == (date(2026, 11, 24), None, None)
    assert not any(w.startswith("time_lookup:") for w in warnings)


@pytest.mark.parametrize(
    ("page_start", "local_start"),
    [
        ("2026-11-24T18:00:00-05:00", time(18)),  # the town's own offset
        ("2026-11-24T23:00:00Z", time(18)),  # the same instant written in UTC
        # 9 PM in Bloomington is already the next day in UTC: still Nov 24 here.
        ("2026-11-25T02:00:00+00:00", time(21)),
        ("2026-11-24T18:00:00-08:00", time(21)),  # a page written in Pacific time
    ],
)
@pytest.mark.asyncio
async def test_page_times_are_read_on_the_towns_clock(page_start, local_start):
    fetch = _PageFetch({HUNT: _detail_page(HUNT, page_start)})
    (filled,), _ = await fill_from_event_pages(
        [_listing(HUNT)], fetch, FetchConfig(), timezone=INDY
    )
    assert _local(filled) == (date(2026, 11, 24), local_start, None)


@pytest.mark.asyncio
async def test_page_about_another_day_leaves_time_tbd():
    # 2 AM UTC on Nov 24 is still Nov 23 in Bloomington: another occurrence.
    fetch = _PageFetch({HUNT: _detail_page(HUNT, "2026-11-24T02:00:00Z")})
    (kept,), _ = await fill_from_event_pages([_listing(HUNT)], fetch, FetchConfig(), timezone=INDY)
    assert kept.raw["start_datetime"] == "2026-11-24"


@pytest.mark.asyncio
async def test_each_daily_listing_of_a_series_gets_its_own_days_time():
    days = {
        f"https://www.eventbrite.com/e/bloomington-indiana-scavenger-hunt-tickets-19962700{n}": d
        for n, d in (("31402", "2026-11-23"), ("33408", "2026-11-24"), ("35414", "2026-11-25"))
    }
    pages = {url: _detail_page(url, f"{d}T10:00:00-05:00") for url, d in days.items()}
    # One page is about a different day than its listing: that one stays TBD.
    late = next(iter(days))
    pages[late] = _detail_page(late, "2026-11-30T10:00:00-05:00")
    fetch = _PageFetch(pages)
    listings = [_listing(url, d) for url, d in days.items()]
    result, warnings = await fill_from_event_pages(listings, fetch, FetchConfig(), timezone=INDY)
    assert fetch.fetched == list(days)  # one fetch per listing, no repeats
    assert [_local(c) for c in result] == [
        (date(2026, 11, 23), None, None),
        (date(2026, 11, 24), time(10), None),
        (date(2026, 11, 25), time(10), None),
    ]
    assert "time_lookup:2_filled_of_3_pages" in warnings


@pytest.mark.asyncio
async def test_skipped_listings_cost_no_fetch_and_no_budget():
    fetch = _PageFetch({
        INDY_BREWFEST: _detail_page(INDY_BREWFEST, "2026-11-24T12:00:00-05:00"),
        HUNT: _detail_page(HUNT, "2026-11-24T10:00:00-05:00"),
    })
    away = _listing(INDY_BREWFEST, address="10 W Market St, Indianapolis, IN, 46204")
    result, warnings = await fill_from_event_pages(
        [away, _listing(HUNT)], fetch, FetchConfig(), timezone=INDY, max_fetches=1,
        skip=lambda c: "Indianapolis" in c.raw.get("address", ""),
    )
    assert fetch.fetched == [HUNT]
    assert result[0].raw == away.raw
    assert _local(result[1])[1] == time(10)
    assert "event_page_lookup_skipped_out_of_town:1" in warnings
    assert not any(w.startswith("event_page_lookup_budget_reached") for w in warnings)


@pytest.mark.asyncio
async def test_lookup_budget_is_respected():
    urls = [f"https://www.eventbrite.com/e/event-tickets-{n}" for n in range(5)]
    fetch = _PageFetch({u: _detail_page(u, "2026-11-24T10:00:00-05:00") for u in urls})
    _, warnings = await fill_from_event_pages(
        [_listing(u) for u in urls], fetch, FetchConfig(), timezone=INDY, max_fetches=2
    )
    assert fetch.fetched == urls[:2]
    assert "event_page_lookup_budget_reached:3_events_not_looked_up" in warnings


# --- a whole import --------------------------------------------------------------


def _listing_event(name: str, url: str, town: str, day: str = "2026-11-24") -> dict:
    return {
        "@context": "https://schema.org",
        "@type": "Event",
        "name": name,
        "description": DESCRIPTION,
        "startDate": day,
        "endDate": day,
        "url": url,
        "location": {
            "@type": "Place",
            "name": "The Venue",
            "address": {
                "@type": "PostalAddress",
                "streetAddress": "235 West Dodds Street",
                "addressLocality": town,
                "addressRegion": "IN",
                "postalCode": "47403",
            },
        },
    }


LISTING_EVENTS = [
    _listing_event("Bloomington Indiana Scavenger Hunt", HUNT, "Bloomington"),
    _listing_event("Foodies + New Friends: Bloomington | Giving Back Edition", FOODIES,
                   "Bloomington"),
    _listing_event("Indy Brewfest", INDY_BREWFEST, "Indianapolis"),
]
DETAIL_PAGES = {
    HUNT: _detail_page(HUNT, "2026-11-24T10:00:00-05:00", "2026-11-24T12:00:00-05:00"),
    FOODIES: _detail_page(FOODIES, "2026-11-24T18:30:00-05:00"),
    INDY_BREWFEST: _detail_page(INDY_BREWFEST, "2026-11-24T12:00:00-05:00"),
}


def _site(requests: list[str], pages: dict[str, str] | None = None):
    pages = DETAIL_PAGES if pages is None else pages
    blocks = "".join(
        f'<script type="application/ld+json">{json.dumps(e)}</script>' for e in LISTING_EVENTS
    )
    listing = f"<html><head><title>Events</title>{blocks}</head><body></body></html>"

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        requests.append(url)
        body = pages.get(url, listing if url.startswith(LISTING_URL) else None)
        if body is None:
            return httpx.Response(404, text="")
        return httpx.Response(200, text=body, headers={"content-type": "text/html"})

    return handler


async def _approved_source(db_session, make_city, make_website, make_user):
    city = make_city(
        name="Bloomington", slug="bloomington", state_or_region="Indiana", timezone=INDY
    )
    website = make_website(city, name="Eventbrite", base_url="https://www.eventbrite.com")
    website.configuration = CONFIG.model_dump(mode="json")
    db_session.commit()
    requests: list[str] = []
    with patched_http_fetch(_site(requests)):
        await preview_extraction(db_session, website)
    # Preview stays one fetch of the listing: no event pages.
    assert requests == [LISTING_URL]
    approve_configuration(
        db_session, website, approved_by_user_id=make_user(email="a@example.com").id
    )
    return website


@pytest.mark.asyncio
async def test_import_fills_times_for_in_town_events_only(
    db_session, make_city, make_website, make_user
):
    website = await _approved_source(db_session, make_city, make_website, make_user)
    requests: list[str] = []
    with patched_http_fetch(_site(requests)):
        result = await run_extraction(db_session, website, triggered_by_user_id=None)

    # The Indianapolis listing is left out before its page is ever fetched.
    assert INDY_BREWFEST not in requests
    assert sorted(requests) == sorted([LISTING_URL, HUNT, FOODIES])
    events = {e.canonical_url: e for e in db_session.query(Event)}
    assert set(events) == {HUNT, FOODIES}
    hunt, foodies = events[HUNT], events[FOODIES]
    assert (hunt.start_date, hunt.start_time, hunt.end_time) == (
        date(2026, 11, 24), time(10), time(12)
    )
    assert (foodies.start_date, foodies.start_time) == (date(2026, 11, 24), time(18, 30))
    assert hunt.timezone == INDY and not hunt.all_day
    assert hunt.description == DESCRIPTION and hunt.venue == "The Venue"
    run = db_session.get(ExtractionRun, result.run_id)
    assert "time_lookup:2_filled_of_2_pages" in run.warnings
    assert "event_page_lookup_skipped_out_of_town:1" in run.warnings
    assert "outside_home_town_excluded:1" in run.warnings


@pytest.mark.asyncio
async def test_reimport_keeps_times_and_admin_decisions_without_refetching(
    db_session, make_city, make_website, make_user, make_category
):
    website = await _approved_source(db_session, make_city, make_website, make_user)
    with patched_http_fetch(_site([])):
        await run_extraction(db_session, website, triggered_by_user_id=None)
    hunt = db_session.query(Event).filter_by(canonical_url=HUNT).one()
    foodies = db_session.query(Event).filter_by(canonical_url=FOODIES).one()
    category = make_category(name="Hunts", slug="hunts-test")
    hunt.category_override_id = category.id
    foodies.duplicate_status = "confirmed_duplicate"
    foodies.duplicate_of_id = hunt.id
    db_session.commit()
    ids = (hunt.id, foodies.id)

    requests: list[str] = []
    with patched_http_fetch(_site(requests)):
        await run_extraction(db_session, website, triggered_by_user_id=None)

    # Both already have times, so only the listing is read.
    assert requests == [LISTING_URL]
    db_session.expire_all()
    hunt = db_session.query(Event).filter_by(canonical_url=HUNT).one()
    foodies = db_session.query(Event).filter_by(canonical_url=FOODIES).one()
    assert (hunt.id, foodies.id) == ids
    assert db_session.query(Event).count() == 2
    assert hunt.start_time == time(10) and foodies.start_time == time(18, 30)
    assert hunt.category_override_id == category.id
    assert foodies.duplicate_status == "confirmed_duplicate"
    assert foodies.duplicate_of_id == hunt.id


@pytest.mark.asyncio
async def test_event_whose_page_has_no_time_stays_tbd_and_is_retried_later(
    db_session, make_city, make_website, make_user
):
    website = await _approved_source(db_session, make_city, make_website, make_user)
    pages = {**DETAIL_PAGES, FOODIES: _detail_page(FOODIES, "2026-11-24")}
    with patched_http_fetch(_site([], pages)):
        await run_extraction(db_session, website, triggered_by_user_id=None)
    foodies = db_session.query(Event).filter_by(canonical_url=FOODIES).one()
    assert (foodies.start_date, foodies.start_time, foodies.end_time) == (
        date(2026, 11, 24), None, None
    )

    # Once the page states a time, the next run picks it up.
    requests: list[str] = []
    with patched_http_fetch(_site(requests)):
        await run_extraction(db_session, website, triggered_by_user_id=None)
    assert requests == [LISTING_URL, FOODIES]
    db_session.expire_all()
    assert db_session.query(Event).filter_by(canonical_url=FOODIES).one().start_time == time(
        18, 30
    )


@pytest.mark.asyncio
async def test_page_end_on_another_day_does_not_replace_the_listings_end_date():
    fetch = _PageFetch(
        {HUNT: _detail_page(HUNT, "2026-11-24T10:00:00-05:00", "2026-11-26T12:00:00-05:00")}
    )
    (filled,), _ = await fill_from_event_pages(
        [_listing(HUNT, end_datetime="2026-11-24")], fetch, FetchConfig(), timezone=INDY
    )
    assert filled.raw["end_datetime"] == "2026-11-24"
    assert _local(filled) == (date(2026, 11, 24), time(10), None)
