"""Detail-page lookup for events whose listing carries no description or venue."""

import httpx
import pytest

from app.extraction.event_page_fallback import (
    PageVenue,
    description_from_page,
    fill_from_event_pages,
    venue_from_page,
)
from app.extraction.normalize import normalize_candidate
from app.extraction.types import EventCandidate
from app.models.event import Event
from app.repositories.event import described_event_urls, event_urls_with_venue
from app.schemas.extraction import FetchConfig, SiteConfiguration
from app.services.extraction_runs import preview_extraction, run_extraction
from app.services.website_configuration import approve_configuration
from tests.extraction_helpers import make_response, patched_http_fetch

LONG = "A long enough description of this particular event and what happens at it."
SITE_BLURB = "The official events calendar of Example University and its community."


def _page(*, jsonld: str = "", meta: str | None = None) -> str:
    meta_tag = f'<meta property="og:description" content="{meta}">' if meta else ""
    return f"<html><head>{meta_tag}{jsonld}</head><body><h1>Event</h1></body></html>"


def _jsonld(*events: dict) -> str:
    import json

    return f'<script type="application/ld+json">{json.dumps(list(events))}</script>'


def _candidate(slug: str, **raw) -> EventCandidate:
    return EventCandidate(
        raw={"title": slug.title(), "canonical_url": f"/events/{slug}", **raw},
        title=None,
        canonical_url=None,
        description=None,
        start_date=None,
        start_time=None,
        end_date=None,
        end_time=None,
        timezone=None,
        venue=None,
        address=None,
        image_url=None,
        latitude=None,
        longitude=None,
        source_category=None,
        external_source_id=None,
        field_source_paths={},
        transformation_history=(),
        source_page="https://example.com/events",
        extraction_pattern="generic_html_cards",
        warnings=(),
        raw_record_hash=slug,
    )


class PageFetch:
    def __init__(self, pages: dict[str, str], status: int = 200):
        self.pages, self.status, self.fetched = pages, status, []

    async def fetch(self, request, config):
        self.fetched.append(request.url)
        return make_response(
            self.pages.get(request.url, ""), final_url=request.url, status_code=self.status
        )


# --- reading one page --------------------------------------------------------


def test_jsonld_event_description_wins_over_meta():
    html = _page(jsonld=_jsonld({"@type": "MusicEvent", "description": LONG}), meta=SITE_BLURB)
    assert description_from_page(html, "https://example.com/e/1") == (
        LONG,
        "detail:jsonld.description",
    )


def test_meta_description_used_when_page_has_no_event_jsonld():
    html = _page(meta=LONG)
    assert description_from_page(html, "https://example.com/e/1") == (
        LONG,
        "detail:meta.description",
    )


def test_page_with_several_events_only_trusts_the_one_for_this_url():
    other = {"@type": "Event", "url": "https://example.com/e/2", "description": "Other " + LONG}
    mine = {"@type": "Event", "url": "/e/1/", "description": LONG}
    url = "https://example.com/e/1"
    assert description_from_page(_page(jsonld=_jsonld(other, mine)), url)[0] == LONG
    # Without a URL match it can't tell which event is this page's.
    assert description_from_page(_page(jsonld=_jsonld(other, other)), url) is None


def test_labels_and_title_repeats_are_not_descriptions():
    assert description_from_page(_page(meta="Free"), "https://example.com/e/1") is None
    title = "Spring Concert at the Auditorium Hall"
    assert description_from_page(_page(meta=title), "https://example.com/e/1", title) is None


# --- filling a run's candidates ---------------------------------------------


@pytest.mark.asyncio
async def test_fills_only_events_without_a_description():
    fetch = PageFetch({"https://example.com/events/a": _page(meta=LONG)})
    has_own = _candidate(
        "b", description="Already described by the listing itself.", venue="Hall B"
    )
    has_summary = _candidate(
        "c", summary="A short summary the source provided itself.", venue="Hall C"
    )
    result, warnings = await fill_from_event_pages(
        [_candidate("a"), has_own, has_summary], fetch, FetchConfig()
    )
    assert fetch.fetched == ["https://example.com/events/a"]
    assert result[0].raw["description"] == LONG
    assert result[0].field_source_paths["description"] == "detail:meta.description"
    assert result[1] is has_own and result[2] is has_summary
    assert "description_lookup:1_filled_of_1_pages" in warnings


@pytest.mark.asyncio
async def test_follows_detail_link_and_skips_urls_already_described():
    fetch = PageFetch({"https://tickets.example.com/a": _page(meta=LONG)})
    result, _ = await fill_from_event_pages(
        [
            _candidate("a", detail_link="https://tickets.example.com/a"),
            _candidate("b", venue="Hall B"),
        ],
        fetch,
        FetchConfig(),
        known_described_urls=frozenset({"https://example.com/events/b"}),
    )
    assert fetch.fetched == ["https://tickets.example.com/a"]
    assert result[0].raw["description"] == LONG
    assert "description" not in result[1].raw


@pytest.mark.asyncio
async def test_site_wide_meta_description_is_dropped_as_boilerplate():
    pages = {
        "https://example.com/events/a": _page(meta=SITE_BLURB),
        "https://example.com/events/b": _page(meta=SITE_BLURB),
        "https://example.com/events/c": _page(meta="Listing-wide " + SITE_BLURB),
        "https://example.com/events/d": _page(
            jsonld=_jsonld({"@type": "Event", "description": LONG})
        ),
    }
    result, _ = await fill_from_event_pages(
        [_candidate(s) for s in "abcd"],
        PageFetch(pages),
        FetchConfig(),
        listing_html=_page(meta="Listing-wide " + SITE_BLURB),
    )
    assert [c.raw.get("description") for c in result] == [None, None, None, LONG]


@pytest.mark.asyncio
async def test_budget_caps_fetches_and_failures_leave_events_untouched():
    fetch = PageFetch({}, status=404)
    candidates = [_candidate(s) for s in "abc"]
    result, warnings = await fill_from_event_pages(
        candidates, fetch, FetchConfig(), max_fetches=2
    )
    assert len(fetch.fetched) == 2
    assert result == candidates
    assert "event_page_lookup_budget_reached:1_events_not_looked_up" in warnings


def test_normalize_falls_back_to_source_summary():
    config = SiteConfiguration(pattern_name="livewhale_json", listing_url="https://example.com")
    candidate = _candidate("a", description="  ", summary="<p>The source's own summary.</p>")
    assert normalize_candidate(candidate, config).description == "The source's own summary."


# --- end to end through a persistent run --------------------------------------

LISTING = f"""<html><head><meta name="description" content="{SITE_BLURB}"></head><body>
<div class="card"><h3><a href="/events/talk">Guest Talk</a></h3>
<span class="date">2030-05-01</span></div>
<div class="card"><h3><a href="/events/show">Spring Show</a></h3>
<span class="date">2030-05-02</span></div></body></html>"""


@pytest.mark.asyncio
async def test_run_extraction_stores_descriptions_from_event_pages(
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
            "start_datetime": {"kind": "css", "selector": ".date"},
        },
        date_formats=["%Y-%m-%d"],
    ).model_dump(mode="json")
    db_session.commit()

    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.path == "/events/talk":
            body = _page(jsonld=_jsonld({"@type": "Event", "description": LONG}))
        elif request.url.path == "/events/show":
            body = _page(meta=SITE_BLURB)  # just the site blurb: not a description
        else:
            body = LISTING
        return httpx.Response(200, text=body, headers={"content-type": "text/html"})

    with patched_http_fetch(handler):
        await preview_extraction(db_session, website)
        # Preview stays a single fetch of the listing.
        assert requests == ["https://example.com/events"]
        approve_configuration(
            db_session, website, approved_by_user_id=make_user(email="a@example.com").id
        )
        await run_extraction(db_session, website, triggered_by_user_id=None)
        requests.clear()
        await run_extraction(db_session, website, triggered_by_user_id=None)

    events = {e.title: e.description for e in db_session.query(Event)}
    assert events == {"Guest Talk": LONG, "Spring Show": None}
    assert described_event_urls(db_session, website.id) == {"https://example.com/events/talk"}
    # The second run re-checks both pages: neither has said where the event is,
    # and "show" still has no description either.
    assert requests == [
        "https://example.com/events",
        "https://example.com/events/talk",
        "https://example.com/events/show",
    ]


# --- venues --------------------------------------------------------------------


def _place(name=None, **address):
    place = {"@type": "Place"}
    if name is not None:
        place["name"] = name
    if address:
        place["address"] = {"@type": "PostalAddress", **address}
    return place


def test_venue_from_jsonld_place_with_address():
    event = {
        "@type": "Event",
        "location": _place(
            "Memorial Hall", streetAddress="1 Main St", addressLocality="Bloomington"
        ),
    }
    assert venue_from_page(_page(jsonld=_jsonld(event)), "https://example.com/e/1") == PageVenue(
        "Memorial Hall", "1 Main St, Bloomington", "detail:jsonld.location"
    )


def test_venue_from_jsonld_string_and_hybrid_list():
    url = "https://example.com/e/1"
    as_string = {"@type": "Event", "location": "Zoellner Arts Center"}
    assert venue_from_page(_page(jsonld=_jsonld(as_string)), url).venue == "Zoellner Arts Center"
    hybrid = {
        "@type": "Event",
        "location": [{"@type": "VirtualLocation", "url": "https://zoom.us/j/1"}, _place("Room 5")],
    }
    assert venue_from_page(_page(jsonld=_jsonld(hybrid)), url).venue == "Room 5"
    online = {"@type": "Event", "location": {"@type": "VirtualLocation", "url": "https://x"}}
    assert venue_from_page(_page(jsonld=_jsonld(online)), url) is None


def test_placeholder_venue_on_the_page_is_not_a_venue():
    event = {"@type": "Event", "location": _place("TBA")}
    assert venue_from_page(_page(jsonld=_jsonld(event)), "https://example.com/e/1") is None


def test_venue_from_microdata_only_when_the_page_has_one_location():
    one = (
        '<html><body><div itemscope itemtype="https://schema.org/Event">'
        '<div itemprop="location" itemscope itemtype="https://schema.org/Place">'
        '<span itemprop="name">The Bluebird</span>'
        '<span itemprop="address">216 N Walnut St</span></div></div></body></html>'
    )
    assert venue_from_page(one, "https://example.com/e/1") == PageVenue(
        "The Bluebird", "216 N Walnut St", "detail:microdata.location"
    )
    two = one.replace("</div></div>", '</div><div itemprop="location">Elsewhere</div></div>')
    assert venue_from_page(two, "https://example.com/e/1") is None


@pytest.mark.asyncio
async def test_fills_missing_and_placeholder_venues_but_keeps_real_ones():
    page = _page(jsonld=_jsonld({"@type": "Event", "description": LONG, "location": _place(
        "Memorial Hall", streetAddress="1 Main St"
    )}))
    fetch = PageFetch({f"https://example.com/events/{s}": page for s in "abc"})
    has_venue = _candidate("c", venue="Own Venue", description=LONG)
    result, warnings = await fill_from_event_pages(
        [_candidate("a"), _candidate("b", venue="TBA", description=LONG), has_venue],
        fetch,
        FetchConfig(),
    )
    assert fetch.fetched == ["https://example.com/events/a", "https://example.com/events/b"]
    assert (result[0].raw["venue"], result[0].raw["address"]) == ("Memorial Hall", "1 Main St")
    assert result[0].field_source_paths["venue"] == "detail:jsonld.location"
    assert result[0].raw["description"] == LONG
    assert result[1].raw["venue"] == "Memorial Hall"
    assert result[2] is has_venue
    assert "venue_lookup:2_filled_of_2_pages" in warnings


@pytest.mark.asyncio
async def test_page_is_fetched_only_for_fields_still_missing():
    """An event already stored with a venue but no description is looked up
    for its description only, and the other way round; with both known, the
    page isn't fetched at all."""
    page = _page(
        jsonld=_jsonld({"@type": "Event", "description": LONG, "location": _place("Hall A")})
    )
    fetch = PageFetch({f"https://example.com/events/{s}": page for s in "abc"})
    result, _ = await fill_from_event_pages(
        [_candidate("a"), _candidate("b"), _candidate("c")],
        fetch,
        FetchConfig(),
        known_described_urls=frozenset({"https://example.com/events/a", "https://example.com/events/c"}),
        known_venue_urls=frozenset({"https://example.com/events/b", "https://example.com/events/c"}),
    )
    assert fetch.fetched == ["https://example.com/events/a", "https://example.com/events/b"]
    assert result[0].raw.get("venue") == "Hall A" and "description" not in result[0].raw
    assert result[1].raw.get("description") == LONG and "venue" not in result[1].raw


@pytest.mark.asyncio
async def test_run_extraction_stores_venues_from_event_pages(
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
            "start_datetime": {"kind": "css", "selector": ".date"},
        },
        date_formats=["%Y-%m-%d"],
    ).model_dump(mode="json")
    db_session.commit()

    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.path == "/events/talk":
            body = _page(
                jsonld=_jsonld(
                    {"@type": "Event", "description": LONG, "location": _place("Memorial Hall")}
                )
            )
        elif request.url.path == "/events/show":
            body = _page()  # the page doesn't say where either
        else:
            body = LISTING
        return httpx.Response(200, text=body, headers={"content-type": "text/html"})

    with patched_http_fetch(handler):
        await preview_extraction(db_session, website)
        approve_configuration(
            db_session, website, approved_by_user_id=make_user(email="a@example.com").id
        )
        await run_extraction(db_session, website, triggered_by_user_id=None)
        requests.clear()
        await run_extraction(db_session, website, triggered_by_user_id=None)

    venues = {e.title: e.venue for e in db_session.query(Event)}
    assert venues == {"Guest Talk": "Memorial Hall", "Spring Show": None}
    assert event_urls_with_venue(db_session, website.id) == {"https://example.com/events/talk"}
    # The second run only re-checks the page that still had nothing.
    assert requests == ["https://example.com/events", "https://example.com/events/show"]
