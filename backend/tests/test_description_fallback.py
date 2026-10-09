"""Detail-page description lookup for events whose listing carries none."""

import httpx
import pytest

from app.extraction.description_fallback import description_from_page, fill_missing_descriptions
from app.extraction.normalize import normalize_candidate
from app.extraction.types import EventCandidate
from app.models.event import Event
from app.repositories.event import described_event_urls
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
    has_own = _candidate("b", description="Already described by the listing itself.")
    has_summary = _candidate("c", summary="A short summary the source provided itself.")
    result, warnings = await fill_missing_descriptions(
        [_candidate("a"), has_own, has_summary], fetch, FetchConfig()
    )
    assert fetch.fetched == ["https://example.com/events/a"]
    assert result[0].raw["description"] == LONG
    assert result[0].field_source_paths["description"] == "detail:meta.description"
    assert result[1] is has_own and result[2] is has_summary
    assert warnings == ["description_lookup:1_filled_of_1_pages"]


@pytest.mark.asyncio
async def test_follows_detail_link_and_skips_urls_already_described():
    fetch = PageFetch({"https://tickets.example.com/a": _page(meta=LONG)})
    result, _ = await fill_missing_descriptions(
        [_candidate("a", detail_link="https://tickets.example.com/a"), _candidate("b")],
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
    result, _ = await fill_missing_descriptions(
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
    result, warnings = await fill_missing_descriptions(
        candidates, fetch, FetchConfig(), max_fetches=2
    )
    assert len(fetch.fetched) == 2
    assert result == candidates
    assert "description_lookup_budget_reached:1_events_not_looked_up" in warnings


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
    # The second run doesn't re-fetch the page it already has a description for.
    assert requests == ["https://example.com/events", "https://example.com/events/show"]
