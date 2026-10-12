"""Detection and recovery fixes found while onboarding college-town sources:

* site-chrome JSON (a shared brand footer) is never an event endpoint,
* bot-check markers count only in a page's visible text, so a page that merely
  loads a Cloudflare script or a reCAPTCHA widget is not "blocked",
* a listing page's own calendar feed is discovered and used,
* every browser block reason has a plain-language explanation,
* a rendered listing that plain HTTP cannot read moves to the browser transport.

All fixtures are synthetic; nothing branches on a hostname.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from app.extraction.browser import _challenge_reason
from app.extraction.challenge import find_marker, visible_text
from app.extraction.detection import run_detection
from app.extraction.feed_links import feed_links
from app.extraction.fetch import _blocked_marker, explain_block_reason
from app.extraction.structured_candidates import (
    FIRST_PARTY_EVENT_CANDIDATE,
    FIRST_PARTY_OTHER,
    analyze_response,
)
from app.services.browser_recovery import _http_replay_empty
from app.services.extraction_runs import follow_advertised_feed

from .extraction_helpers import make_response, patched_http_fetch

LISTING = "https://calendars.example.edu/list/33"

# --- site chrome is not an event endpoint ------------------------------------


def _footer_payload():
    return {
        "columns": [
            {"id": 1, "title": "About", "url": "https://www.example.edu/about", "category": "a"},
            {"id": 2, "title": "Contact", "url": "https://www.example.edu/contact"},
            {"id": 3, "title": "Privacy", "url": "https://www.example.edu/privacy"},
        ],
        "total": 3,
    }


def test_brand_footer_json_is_not_an_event_candidate():
    analysis = analyze_response(
        url="https://cdn.brand.example.edu/data/footer.json",
        payload=_footer_payload(),
        listing_url=LISTING,
    )
    assert analysis.first_party
    assert analysis.classification == FIRST_PARTY_OTHER
    assert not analysis.is_event_candidate


def test_dateless_records_are_rejected_even_on_a_neutral_path():
    analysis = analyze_response(
        url="https://www.example.edu/api/links",
        payload=_footer_payload(),
        listing_url=LISTING,
    )
    assert not analysis.is_event_candidate
    assert analysis.reject_reason in (
        "records carry no event date field",
        "first-party JSON but below the event-likeness threshold",
    )


def test_real_event_api_still_qualifies():
    payload = {
        "content": [
            {"id": n, "title": f"E{n}", "startDate": "2026-11-01", "url": f"/e/{n}"}
            for n in range(5)
        ],
        "totalPages": 2,
    }
    analysis = analyze_response(
        url="https://events.example.edu/api/events", payload=payload, listing_url=LISTING
    )
    assert analysis.classification == FIRST_PARTY_EVENT_CANDIDATE


# --- challenge markers use visible text only ----------------------------------

ORDINARY_PAGE = """<!doctype html><html><head><title>Theater - Upcoming Shows</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/jquery/3.7.1/jquery.min.js"></script>
<script src="https://www.google.com/recaptcha/api.js"></script>
<!-- cloudflare insights -->
<link rel="preconnect" href="https://challenges.cloudflare.com">
</head><body><h1>Upcoming shows</h1>
<form><p>This site is protected by reCAPTCHA.</p></form></body></html>"""

CHALLENGE_PAGE = """<html><head><title>Just a moment...</title></head><body>
<h1>Checking your browser before accessing the site.</h1>
<p>Performance &amp; security by Cloudflare</p></body></html>"""


def test_ordinary_page_loading_vendor_scripts_is_not_blocked():
    assert _blocked_marker(200, ORDINARY_PAGE.encode(), {}) is None
    assert _challenge_reason(ORDINARY_PAGE, 200) is None
    response = make_response(ORDINARY_PAGE, final_url=LISTING)
    assert "blocked" not in run_detection(response).evidence


def test_real_challenge_page_is_still_blocked():
    assert _blocked_marker(200, CHALLENGE_PAGE.encode(), {}) == "challenge_marker:cloudflare"
    assert _challenge_reason(CHALLENGE_PAGE, 200) is not None
    assert _challenge_reason("", 403) == "http_403"


def test_visible_text_drops_scripts_comments_and_attributes():
    text = visible_text('<a href="/captcha">Go</a><script>captcha()</script><!-- captcha -->')
    assert text == "go"
    assert find_marker("<p>Please complete the CAPTCHA</p>", ("captcha",)) == "captcha"
    assert find_marker("<p>Protected by reCAPTCHA</p>", ("captcha",)) is None


# --- advertised calendar feeds -------------------------------------------------

LISTING_WITH_FEEDS = """<html><head>
<link rel="alternate" type="application/rss+xml" href="/rss/33.xml">
</head><body>
<a href="/detail/33?eventId=99">One event</a>
<a href="/ical/33.ics?eventId=99">Add this event</a>
<a href="webcal://calendars.example.edu/ical/33.ics">Subscribe (iCal)</a>
<a href="https://social.example.com/share">Share</a>
</body></html>"""

ICS_FEED = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:a@example.edu
SUMMARY:Recital
DTSTART:20261101T190000
END:VEVENT
BEGIN:VEVENT
UID:b@example.edu
SUMMARY:Gala
DTSTART:20261108T190000
END:VEVENT
END:VCALENDAR
"""


def test_feed_links_rank_ical_first_and_skip_single_event_downloads():
    links = feed_links(LISTING_WITH_FEEDS, LISTING)
    assert links == [
        "https://calendars.example.edu/ical/33.ics",
        "https://calendars.example.edu/rss/33.xml",
    ]
    assert "https://calendars.example.edu/ical/33.ics?eventId=99" not in links
    assert all("social.example.com" not in link for link in links)


def test_declared_ical_beats_declared_rss():
    html = (
        '<link rel="alternate" type="application/rss+xml" href="/feed.rss">'
        '<link rel="alternate" type="text/calendar" href="/cal.ics">'
    )
    assert feed_links(html, LISTING)[0] == "https://calendars.example.edu/cal.ics"


def test_page_without_feeds_advertises_nothing():
    assert feed_links("<html><body><a href='/about'>About</a></body></html>", LISTING) == []


@pytest.mark.asyncio
async def test_follow_advertised_feed_fetches_and_detects_the_feed():
    html = '<html><body><a href="/ical/33.ics">iCal</a></body></html>'

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/ical/33.ics"
        return httpx.Response(200, text=ICS_FEED, headers={"content-type": "text/calendar"})

    with patched_http_fetch(handler):
        followed = await follow_advertised_feed(make_response(html, final_url=LISTING))
    assert followed is not None
    response, detection = followed
    assert detection.pattern_name == "ics_calendar"
    assert response.final_url == "https://calendars.example.edu/ical/33.ics"


@pytest.mark.asyncio
async def test_single_event_feed_is_not_taken_for_the_calendar():
    html = '<html><body><a href="/ical/33.ics">iCal</a></body></html>'
    one_event = ICS_FEED.split("BEGIN:VEVENT\nUID:b")[0] + "END:VCALENDAR\n"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=one_event, headers={"content-type": "text/calendar"})

    with patched_http_fetch(handler):
        assert await follow_advertised_feed(make_response(html, final_url=LISTING)) is None


@pytest.mark.asyncio
async def test_blocked_page_is_never_followed():
    page = make_response("<a href='/x.ics'>x</a>", final_url=LISTING, blocked_reason="http_403")
    assert await follow_advertised_feed(page) is None


# --- explanations and browser fallback ----------------------------------------


@pytest.mark.parametrize(
    "reason, fragment",
    [
        ("http_403", "refused the request (HTTP 403)"),
        ("http_429", "rate-limited"),
        ("challenge_marker:captcha", "bot check"),
        ("login_wall:please log in", "sign in"),
        ("browser_error:TargetClosedError", "running the browser on Bulletin's server"),
        ("ssrf_blocked:10.0.0.1", "private or disallowed network"),
        ("edge_protection:http_403", "edge/bot management"),
        ("something_new", "Blocked: something_new"),
    ],
)
def test_every_block_reason_is_explained(reason, fragment):
    assert fragment in explain_block_reason(reason)


def test_explain_block_reason_none():
    assert explain_block_reason(None) is None


def _preview(found: int):
    return SimpleNamespace(result=SimpleNamespace(events_found=found))


def test_rendered_listing_with_no_http_events_needs_the_browser():
    assert _http_replay_empty(_preview(0), "rendered_html")
    assert not _http_replay_empty(_preview(4), "rendered_html")
    assert not _http_replay_empty(_preview(0), "structured_api")


# --- a source URL that is itself The Events Calendar REST collection ----------

TRIBE_URL = "https://visit.example.com/wp-json/tribe/events/v1/events"


def _tribe_payload(events):
    return {
        "events": events,
        "rest_url": TRIBE_URL + "?page=1",
        "next_rest_url": TRIBE_URL + "?page=2",
        "total": 40,
        "total_pages": 2,
    }


def _tribe_event(n):
    return {
        "id": n,
        "title": f"Event {n}",
        "url": f"https://visit.example.com/event/{n}/",
        "start_date": "2026-11-01 19:00:00",
        "end_date": "2026-11-01 21:00:00",
        "utc_start_date": "2026-11-02 01:00:00",
        "all_day": False,
    }


def test_direct_tribe_rest_url_is_detected_as_the_events_calendar():
    import json

    body = json.dumps(_tribe_payload([_tribe_event(1), _tribe_event(2)]))
    response = make_response(body, final_url=TRIBE_URL, content_type="application/json")
    detection = run_detection(response)
    assert detection.pattern_name == "the_events_calendar"
    assert detection.discovered_endpoints == (TRIBE_URL,)


def test_other_events_json_is_not_taken_for_tribe():
    import json

    body = json.dumps({"events": [{"id": 1, "date_ts": 1, "title": "x"}], "total_pages": 1})
    response = make_response(body, final_url=TRIBE_URL, content_type="application/json")
    assert run_detection(response).pattern_name != "the_events_calendar"


# --- one parse per detection run ------------------------------------------------


def test_detectors_share_one_parse_of_the_page(monkeypatch):
    import app.extraction.detection as detection_module

    real = detection_module.BeautifulSoup
    calls = []

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(detection_module, "BeautifulSoup", counting)
    page = "<html><body>" + "<div class='card'><h3>Show</h3><p>Oct 20</p></div>" * 50
    detection_module.run_all_detectors(make_response(page + "</body></html>"))
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_feed_attempts_are_explained():
    html = '<html><body><a href="/ical/33.ics">iCal</a><a href="/rss/33.xml">RSS</a></body></html>'

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(".ics"):
            return httpx.Response(404, text="gone")
        return httpx.Response(200, text=ICS_FEED, headers={"content-type": "text/calendar"})

    notes: list[str] = []
    with patched_http_fetch(handler):
        followed = await follow_advertised_feed(make_response(html, final_url=LISTING), notes)
    assert followed is not None
    assert notes == [
        "feed https://calendars.example.edu/ical/33.ics: HTTP 404",
        "feed https://calendars.example.edu/rss/33.xml: used (ics_calendar, 2 entries)",
    ]

    none: list[str] = []
    assert await follow_advertised_feed(make_response("<p>hi</p>", final_url=LISTING), none) is None
    assert none == ["feed: the page links no iCalendar or RSS/Atom feed"]


@pytest.mark.parametrize(
    "message, reason, fragment",
    [
        (
            "Page.goto: net::ERR_HTTP2_PROTOCOL_ERROR at https://venue.example.org/",
            "browser_error:net::ERR_HTTP2_PROTOCOL_ERROR",
            "dropped the browser's connection",
        ),
        (
            "Page.goto: net::ERR_NAME_NOT_RESOLVED at https://x.example.org/",
            "browser_error:net::ERR_NAME_NOT_RESOLVED",
            "address could not be found",
        ),
        (
            "Page.goto: net::ERR_CONNECTION_TIMED_OUT at https://x.example.org/",
            "browser_error:net::ERR_CONNECTION_TIMED_OUT",
            "did not respond in time",
        ),
        (
            "Page.goto: net::ERR_ABORTED at https://x.example.org/",
            "browser_error:net::ERR_ABORTED",
            "Bulletin's browser stopped the request",
        ),
        (
            "Browser closed.\nmore detail",
            "browser_error:Exception: Browser closed.",
            "running the browser on Bulletin's server",
        ),
    ],
)
def test_browser_errors_keep_their_detail(message, reason, fragment):
    from app.extraction.browser import browser_error_reason

    assert browser_error_reason(Exception(message)) == reason
    assert fragment in explain_block_reason(reason)


def test_no_draft_reason_names_what_inference_could_not_find():
    from app.extraction.inference.types import InferenceResult
    from app.services.browser_recovery import no_draft_reason

    def result(**kwargs):
        base = dict(
            outcome="needs_review", pattern_name="generic_html_cards", detection_confidence=0.6,
            proposal_confidence=0.0, configuration=None, field_candidates=(),
            date_format_candidates=(), missing_required_fields=(), warnings=(), notes=(),
        )
        return InferenceResult(**{**base, **kwargs})

    assert no_draft_reason(result(missing_required_fields=("start_date",))) == (
        "could not find start date"
    )
    assert no_draft_reason(result(error="no repeated event card found")) == (
        "no repeated event card found"
    )
    assert "needs_review" in no_draft_reason(result())
