"""Event-page venue lookup: the no-venue memory, fetch outcomes, the detail
selectors' canonical-link fallback and lenient JSON-LD."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.extraction.detail_pages import enrich_with_detail_pages
from app.extraction.event_page_fallback import fill_from_event_pages, venue_from_page
from app.models.event import Event
from app.models.event_page_check import EventPageCheck
from app.repositories.event_page_checks import (
    NO_VENUE_MEMORY_DAYS,
    recent_no_venue_urls,
    record_no_venue_pages,
)
from app.schemas.extraction import FetchConfig, SiteConfiguration
from app.services.extraction_runs import preview_extraction, run_extraction
from app.services.website_configuration import approve_configuration
from tests.extraction_helpers import make_response, patched_http_fetch
from tests.test_event_page_fallback import LONG, _candidate, _jsonld, _page, _place

URL = "https://example.com/events/{}"


class Fetch:
    """Serves `pages` by URL; `status` and `blocked` override per URL."""

    def __init__(self, pages=None, *, status=None, blocked=None):
        self.pages = pages or {}
        self.status = status or {}
        self.blocked = blocked or {}
        self.fetched: list[str] = []

    async def fetch(self, request, config):
        self.fetched.append(request.url)
        return make_response(
            self.pages.get(request.url, ""),
            final_url=request.url,
            status_code=self.status.get(request.url, 200),
            blocked_reason=self.blocked.get(request.url),
        )


def _described(slug: str):
    # Has a description already, so its page is only wanted for a venue.
    return _candidate(slug, description=LONG)


# --- the memory, in the lookup -------------------------------------------------


@pytest.mark.asyncio
async def test_page_read_without_a_venue_is_reported_for_the_memory():
    fetch = Fetch({URL.format("a"): _page(), URL.format("b"): _page(
        jsonld=_jsonld({"@type": "Event", "location": _place("Hall B")})
    )})
    checked: set[str] = set()
    result, warnings = await fill_from_event_pages(
        [_described("a"), _described("b")], fetch, FetchConfig(), checked_without_venue=checked
    )
    assert checked == {URL.format("a")}
    assert result[1].raw["venue"] == "Hall B"
    assert "venue_lookup:1_filled_of_2_pages" in warnings
    assert "venue_lookup_page_names_no_venue:1_pages" in warnings


@pytest.mark.asyncio
async def test_recently_checked_pages_are_skipped_so_the_budget_moves_on():
    fetch = Fetch()
    candidates = [_described(s) for s in "abcd"]
    _, warnings = await fill_from_event_pages(
        candidates,
        fetch,
        FetchConfig(),
        recently_checked_venue_urls=frozenset({URL.format("a"), URL.format("b")}),
        max_fetches=2,
    )
    assert fetch.fetched == [URL.format("c"), URL.format("d")]
    assert "venue_lookup_recently_checked:2_events_skipped" in warnings
    assert not any("budget_reached" in w for w in warnings)


@pytest.mark.asyncio
async def test_memory_only_covers_the_venue():
    # Still missing a description: the page is read again for that.
    fetch = Fetch({URL.format("a"): _page(jsonld=_jsonld({"@type": "Event", "description": LONG}))})
    checked: set[str] = set()
    result, _ = await fill_from_event_pages(
        [_candidate("a")],
        fetch,
        FetchConfig(),
        recently_checked_venue_urls=frozenset({URL.format("a")}),
        checked_without_venue=checked,
    )
    assert fetch.fetched == [URL.format("a")]
    assert result[0].raw["description"] == LONG
    assert checked == set()  # the venue wasn't asked of this page


@pytest.mark.asyncio
async def test_failed_and_blocked_fetches_are_counted_apart_and_not_remembered():
    fetch = Fetch(
        {URL.format("c"): _page()},
        status={URL.format("a"): 503},
        blocked={URL.format("b"): "blocked:challenge_page"},
    )
    checked: set[str] = set()
    result, warnings = await fill_from_event_pages(
        [_described(s) for s in "abc"], fetch, FetchConfig(), checked_without_venue=checked
    )
    assert checked == {URL.format("c")}
    assert all("venue" not in c.raw for c in result)
    assert "event_page_lookup_fetch_failed:1_pages" in warnings
    assert "event_page_lookup_fetch_blocked:1_pages" in warnings
    assert "venue_lookup_page_names_no_venue:1_pages" in warnings


@pytest.mark.asyncio
async def test_an_address_alone_still_counts_as_no_venue():
    page = _page(jsonld=_jsonld({"@type": "Event", "location": _place(streetAddress="1 Main St")}))
    checked: set[str] = set()
    result, _ = await fill_from_event_pages(
        [_described("a")], Fetch({URL.format("a"): page}), FetchConfig(),
        checked_without_venue=checked,
    )
    assert result[0].raw.get("address")
    assert checked == {URL.format("a")}


# --- the memory, stored ----------------------------------------------------------


def test_memory_expires_and_is_pruned(db_session, make_city, make_website):
    website = make_website(make_city(), name="Feed", base_url="https://example.com")
    now = datetime(2030, 5, 1, tzinfo=UTC)
    record_no_venue_pages(db_session, website.id, [URL.format("a")], now=now)
    db_session.commit()
    assert recent_no_venue_urls(db_session, website.id, now=now) == {URL.format("a")}

    later = now + timedelta(days=NO_VENUE_MEMORY_DAYS, hours=1)
    assert recent_no_venue_urls(db_session, website.id, now=later) == frozenset()

    # Recording again prunes the expired row and refreshes a re-read page.
    record_no_venue_pages(db_session, website.id, [URL.format("b")], now=later)
    db_session.commit()
    assert {r.url for r in db_session.query(EventPageCheck)} == {URL.format("b")}
    record_no_venue_pages(db_session, website.id, [URL.format("b")], now=later + timedelta(days=1))
    db_session.commit()
    assert db_session.query(EventPageCheck).count() == 1


LISTING = """<html><body>
<div class="card"><h3><a href="/events/talk">Guest Talk</a></h3>
<p class="desc">A long enough description of the guest talk and its speaker.</p>
<span class="date">2030-05-01</span></div></body></html>"""


@pytest.mark.asyncio
async def test_import_remembers_pages_without_a_venue_for_a_week(
    db_session, make_city, make_website, make_user
):
    website = make_website(make_city(), name="Cards", base_url="https://example.com")
    website.configuration = SiteConfiguration(
        pattern_name="generic_html_cards",
        listing_url="https://example.com/events",
        event_container_selector=".card",
        field_selectors={
            "title": {"kind": "css", "selector": "h3 a"},
            "canonical_url": {"kind": "css", "selector": "h3 a", "attribute": "href"},
            "description": {"kind": "css", "selector": ".desc"},
            "start_datetime": {"kind": "css", "selector": ".date"},
        },
        date_formats=["%Y-%m-%d"],
    ).model_dump(mode="json")
    db_session.commit()

    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        body = _page() if request.url.path == "/events/talk" else LISTING
        return httpx.Response(200, text=body, headers={"content-type": "text/html"})

    with patched_http_fetch(handler):
        await preview_extraction(db_session, website)
        approve_configuration(
            db_session, website, approved_by_user_id=make_user(email="a@example.com").id
        )
        requests.clear()
        await run_extraction(db_session, website, triggered_by_user_id=None)
        assert requests == ["https://example.com/events", URL.format("talk")]
        requests.clear()
        await run_extraction(db_session, website, triggered_by_user_id=None)
        assert requests == ["https://example.com/events"]

        # A week on, the page is read again.
        row = db_session.query(EventPageCheck).one()
        row.checked_at = datetime.now(UTC) - timedelta(days=NO_VENUE_MEMORY_DAYS + 1)
        db_session.commit()
        requests.clear()
        await run_extraction(db_session, website, triggered_by_user_id=None)
        assert requests == ["https://example.com/events", URL.format("talk")]

    assert db_session.query(Event).one().venue is None


# --- detail selectors fall back to the canonical link -----------------------------


def _detail_config(**extra) -> SiteConfiguration:
    return SiteConfiguration(
        pattern_name="rss_atom_events",
        listing_url="https://example.com/feed.xml",
        field_selectors={"detail_venue": {"kind": "css", "selector": ".location"}},
        **extra,
    )


@pytest.mark.asyncio
async def test_detail_selectors_read_the_canonical_page_when_there_is_no_detail_link():
    fetch = Fetch({URL.format("a"): '<p class="location">Main Building, Room 2</p>'})
    enriched = await enrich_with_detail_pages([_described("a")], fetch, _detail_config())
    assert fetch.fetched == [URL.format("a")]
    assert enriched[0].raw["venue"] == "Main Building, Room 2"
    assert enriched[0].field_source_paths["venue"].startswith("detail:")


@pytest.mark.asyncio
async def test_detail_link_still_wins_over_the_canonical_link():
    candidate = _candidate("a", detail_link="https://example.com/details/a")
    fetch = Fetch({"https://example.com/details/a": '<p class="location">Hall</p>'})
    enriched = await enrich_with_detail_pages([candidate], fetch, _detail_config())
    assert fetch.fetched == ["https://example.com/details/a"]
    assert enriched[0].raw["venue"] == "Hall"


@pytest.mark.asyncio
async def test_canonical_fallback_respects_the_detail_budget_and_url_schemes():
    candidates = [
        _candidate("a"),
        _candidate("b"),
        _candidate("m", canonical_url="mailto:events@example.com"),
    ]
    fetch = Fetch()
    enriched = await enrich_with_detail_pages(
        candidates, fetch, _detail_config(max_detail_fetches=1)
    )
    assert fetch.fetched == [URL.format("a")]
    assert any("detail_page_fetch_skipped_max_reached" in w for w in enriched[1].warnings)
    assert enriched[2] == candidates[2]


# --- JSON-LD with raw line breaks ---------------------------------------------------


def test_json_ld_with_raw_line_breaks_in_strings_is_read():
    block = json.dumps({"@type": "Event", "location": _place("Hill Auditorium")})
    block = block.replace("Hill Auditorium", "Hill\nAuditorium")
    assert "\n" in block
    page = f'<script type="application/ld+json">{block}</script>'
    found = venue_from_page(page, URL.format("a"))
    assert found is not None and found.venue.split() == ["Hill", "Auditorium"]


def test_json_ld_that_is_truly_broken_is_still_skipped():
    page = '<script type="application/ld+json">{"@type": "Event", "location": </script>'
    assert venue_from_page(page, URL.format("a")) is None
