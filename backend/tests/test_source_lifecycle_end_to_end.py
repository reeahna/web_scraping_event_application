"""What a visitor sees as one source is checked again and again: its events
change, drop out of the feed, come back, or the source sends something broken.

Each test runs real imports (the whole fetch, detect, extract, validate and
save pipeline, with only the network faked) and then reads the public town
and event pages, the way a visitor would. The unit tests in
test_event_freshness.py and test_extraction_*.py cover the pieces; these cover
the whole path from a source's response to the page.
"""

import json
import re
from datetime import date, timedelta

import httpx
import pytest

from app.core.onboarding import ACTIVE
from app.models.event import Event
from app.models.extraction_run import ExtractionRun
from app.schemas.extraction import SiteConfiguration
from app.services.extraction_runs import preview_extraction, run_extraction
from app.services.website_configuration import approve_configuration
from app.services.websites import transition_website
from tests.extraction_helpers import patched_http_fetch

CONFIG = SiteConfiguration(pattern_name="json_ld_event", listing_url="https://example.com/events")
FUTURE = date.today() + timedelta(days=30)
HTML = {"Accept": "text/html"}


def _event(name: str, slug: str, **overrides) -> dict:
    item = {
        "@context": "https://schema.org",
        "@type": "Event",
        "name": name,
        "startDate": f"{FUTURE.isoformat()}T19:00:00",
        "endDate": f"{FUTURE.isoformat()}T21:00:00",
        "url": f"https://example.com/events/{slug}",
        "location": {"@type": "Place", "name": "Buskirk-Chumley Theater"},
    }
    item.update(overrides)
    return item


JAZZ = _event("Jazz Night", "jazz")
SLAM = _event("Poetry Slam", "slam")


class Source:
    """A fake source whose response a test changes between checks."""

    def __init__(self, *events: dict):
        self.events = list(events)
        self.raw: tuple[int, bytes, str] | None = None
        self.error: Exception | None = None

    def page(self, events) -> str:
        scripts = "".join(
            f'<script type="application/ld+json">{json.dumps(e)}</script>' for e in events
        )
        return f"<!DOCTYPE html><html><head><title>Events</title>{scripts}</head></html>"

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.error is not None:
            raise self.error
        if self.raw is not None:
            status, body, ctype = self.raw
            return httpx.Response(status, content=body, headers={"content-type": ctype})
        return httpx.Response(
            200, text=self.page(self.events), headers={"content-type": "text/html"}
        )


@pytest.fixture
def town(make_city):
    return make_city(
        name="Bloomington", slug="bloomington-in", state_or_region="IN",
        timezone="America/Indiana/Indianapolis",
    )


@pytest.fixture
async def site(db_session, town, make_website, make_user):
    website = make_website(town, name="Buskirk-Chumley", base_url="https://example.com")
    website.configuration = CONFIG.model_dump(mode="json")
    db_session.commit()
    approver = make_user(email="approver@example.com")
    with patched_http_fetch(Source(JAZZ).handler):
        await preview_extraction(db_session, website)
    approve_configuration(db_session, website, approved_by_user_id=approver.id)
    transition_website(db_session, website, ACTIVE)
    return website


async def check(db_session, website, source: Source):
    """One import, as the scheduler runs it."""
    with patched_http_fetch(source.handler):
        result = await run_extraction(db_session, website, triggered_by_user_id=None)
    db_session.expire_all()
    return result


def text(html: str) -> str:
    """The words a visitor reads: no tags, and no script blocks (the page's
    JSON-LD repeats every title for search engines)."""
    html = re.sub(r"<script\b.*?</script>", " ", html, flags=re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def town_page(client) -> str:
    resp = client.get("/city/bloomington-in", headers=HTML)
    assert resp.status_code == 200
    return text(resp.text)


def event_id(db_session, title: str) -> int:
    return db_session.query(Event).filter_by(title=title).one().id


# --- A source whose events change ------------------------------------------------


@pytest.mark.asyncio
async def test_changed_details_replace_the_old_ones_on_every_page(client, db_session, site):
    source = Source(JAZZ)
    await check(db_session, site, source)
    jazz_id = event_id(db_session, "Jazz Night")

    source.events = [
        _event(
            "Jazz Night (moved indoors)", "jazz",
            startDate=f"{FUTURE.isoformat()}T20:30:00",
            location={"@type": "Place", "name": "Monroe County Public Library"},
        )
    ]
    result = await check(db_session, site, source)

    assert result.events_changed == 1 and result.events_inserted == 0
    listing = town_page(client)
    assert "Jazz Night (moved indoors)" in listing
    assert listing.count("Jazz Night") == 1  # changed in place, not listed twice
    detail = text(client.get(f"/events/{jazz_id}", headers=HTML).text)
    assert "Monroe County Public Library" in detail
    assert "8:30" in detail
    assert "Buskirk-Chumley Theater" not in detail


@pytest.mark.asyncio
async def test_an_admin_correction_survives_a_later_change_at_the_source(
    client, db_session, site
):
    source = Source(JAZZ)
    await check(db_session, site, source)
    event = db_session.query(Event).one()
    event.corrected_venue = "IU Auditorium"
    db_session.commit()

    source.events = [_event("Jazz Night", "jazz", location={"@type": "Place", "name": "Elsewhere"})]
    await check(db_session, site, source)

    detail = text(client.get(f"/events/{event.id}", headers=HTML).text)
    assert "IU Auditorium" in detail and "Elsewhere" not in detail


# --- A source whose events drop out of the feed ----------------------------------


@pytest.mark.asyncio
async def test_an_event_missing_from_the_feed_stays_listed(client, db_session, site):
    source = Source(JAZZ, SLAM)
    await check(db_session, site, source)

    source.events = [JAZZ]
    result = await check(db_session, site, source)

    assert result.status == "success"
    run = db_session.get(ExtractionRun, result.run_id)
    assert run.events_missing == 1
    listing = town_page(client)
    assert "Jazz Night" in listing and "Poetry Slam" in listing
    slam = client.get(f"/events/{event_id(db_session, 'Poetry Slam')}", headers=HTML)
    assert slam.status_code == 200


@pytest.mark.asyncio
async def test_an_event_that_comes_back_is_not_listed_twice(client, db_session, site):
    source = Source(JAZZ, SLAM)
    await check(db_session, site, source)
    source.events = [JAZZ]
    await check(db_session, site, source)

    source.events = [JAZZ, SLAM]
    result = await check(db_session, site, source)

    assert result.events_inserted == 0
    assert db_session.query(Event).filter_by(title="Poetry Slam").count() == 1
    assert town_page(client).count("Poetry Slam") == 1


@pytest.mark.asyncio
async def test_an_empty_feed_hides_nothing(client, db_session, site):
    source = Source(JAZZ, SLAM)
    await check(db_session, site, source)

    source.events = []
    await check(db_session, site, source)

    listing = town_page(client)
    assert "Jazz Night" in listing and "Poetry Slam" in listing


@pytest.mark.asyncio
async def test_an_event_cancelled_at_the_source_leaves_the_listing(client, db_session, site):
    source = Source(JAZZ, SLAM)
    await check(db_session, site, source)

    source.events = [JAZZ, _event("Poetry Slam", "slam",
                                  eventStatus="https://schema.org/EventCancelled")]
    await check(db_session, site, source)

    listing = town_page(client)
    assert "Jazz Night" in listing and "Poetry Slam" not in listing


# --- A source that fails once and recovers ---------------------------------------


BROKEN_RESPONSES = {
    "server error": (500, b"Internal Server Error", "text/html"),
    "access denied": (403, b"Access Denied", "text/html"),
    "not found": (404, b"Not Found", "text/html"),
    "empty body": (200, b"", "text/html"),
    "not html": (200, b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 20, "image/png"),
    "invalid utf-8": (200, b"<html>\xff\xfe\xfa caf\xe9 </html>", "text/html; charset=utf-8"),
    "broken json-ld": (
        200,
        b'<html><head><script type="application/ld+json">{"@type": "Event", "name": </script>'
        b"</head></html>",
        "text/html",
    ),
    "json instead of html": (200, b'{"error": "maintenance"}', "application/json"),
}


@pytest.mark.parametrize("broken", list(BROKEN_RESPONSES), ids=list(BROKEN_RESPONSES))
@pytest.mark.asyncio
async def test_one_broken_response_keeps_every_event(client, db_session, site, broken):
    source = Source(JAZZ, SLAM)
    await check(db_session, site, source)

    source.raw = BROKEN_RESPONSES[broken]
    await check(db_session, site, source)

    listing = town_page(client)
    assert "Jazz Night" in listing and "Poetry Slam" in listing
    db_session.refresh(site)
    assert site.onboarding_status == "active"


@pytest.mark.parametrize(
    "error",
    [httpx.ConnectTimeout("timed out"), httpx.ConnectError("refused"),
     httpx.ReadError("connection reset")],
    ids=["timeout", "refused", "reset"],
)
@pytest.mark.asyncio
async def test_a_network_failure_keeps_every_event_and_is_counted(
    client, db_session, site, error
):
    source = Source(JAZZ)
    await check(db_session, site, source)

    source.error = error
    result = await check(db_session, site, source)

    assert result.status in ("failed", "blocked")
    db_session.refresh(site)
    assert site.consecutive_failure_count == 1
    assert "Jazz Night" in town_page(client)


@pytest.mark.asyncio
async def test_a_failed_check_is_shown_and_cleared_by_the_next_good_one(
    client, db_session, site
):
    source = Source(JAZZ)
    await check(db_session, site, source)
    assert "could not be checked" not in town_page(client)

    source.raw = BROKEN_RESPONSES["server error"]
    await check(db_session, site, source)
    listing = town_page(client)
    assert "could not be checked" in listing
    assert "Jazz Night" in listing

    source.raw = None
    await check(db_session, site, source)
    db_session.refresh(site)
    assert site.consecutive_failure_count == 0
    assert "could not be checked" not in town_page(client)


# --- A source that sends hostile or absurd event data ----------------------------


@pytest.mark.asyncio
async def test_hostile_event_text_is_shown_as_text(client, db_session, site):
    source = Source(
        _event('<script>alert("x")</script>Jazz <img src=x onerror=alert(1)>', "jazz",
               description='<p onclick="alert(1)">Hi</p><script>alert(2)</script>',
               location={"@type": "Place", "name": "<b>Hall</b>\"'><svg onload=alert(3)>"}),
    )
    await check(db_session, site, source)

    for path in ("/city/bloomington-in", *(f"/events/{e.id}" for e in db_session.query(Event))):
        html = client.get(path, headers=HTML).text
        assert "<script>alert" not in html
        assert "onerror=alert" not in html and "onload=alert" not in html
        assert 'onclick="alert' not in html


@pytest.mark.parametrize(
    "overrides",
    [
        {"url": "javascript:alert(1)"},
        {"url": "http://127.0.0.1/admin"},
        {"url": "file:///etc/passwd"},
        {"startDate": "not a date"},
        {"startDate": "0001-01-01T00:00:00"},
        {"startDate": f"{FUTURE.isoformat()}T19:00:00", "endDate": "1999-01-01T00:00:00"},
        {"name": ""},
        {"name": "x" * 5000},
        {"name": "Jazz\x00Night"},
        {"name": "‮Jazz​﻿"},
        {"geo": {"latitude": 999, "longitude": "nan"}},
    ],
    ids=["js-url", "private-url", "file-url", "bad-date", "year-1", "ends-before-start",
         "no-title", "huge-title", "nul-title", "invisible-chars", "bad-geo"],
)
@pytest.mark.asyncio
async def test_absurd_event_data_never_breaks_the_import_or_the_pages(
    client, db_session, site, overrides
):
    source = Source(JAZZ, {**SLAM, **overrides})
    result = await check(db_session, site, source)

    assert result.status in ("success", "partial")
    assert "Jazz Night" in town_page(client)
    for event in db_session.query(Event):
        assert client.get(f"/events/{event.id}", headers=HTML).status_code in (200, 404)
        assert event.canonical_url.startswith("https://")
    assert client.get("/city/bloomington-in/calendar.ics").status_code in (200, 404)


@pytest.mark.asyncio
async def test_an_over_long_or_nul_title_is_cleaned_and_saved(db_session, site):
    """PostgreSQL refuses a NUL byte or text past its column; one such event
    used to fail the whole import there."""
    source = Source(JAZZ, {**SLAM, "name": "Poetry\x00Slam " + "x" * 5000},
                    _event("Film\x00Club", "film", location={"@type": "Place",
                                                             "name": "v" * 900}))
    result = await check(db_session, site, source)

    assert result.status == "success"
    titles = sorted(e.title for e in db_session.query(Event))
    assert titles[0] == "FilmClub"
    assert titles[2].startswith("PoetrySlam xxx") and len(titles[2]) == 500
    assert "\x00" not in "".join(titles)
    film = db_session.query(Event).filter_by(title="FilmClub").one()
    assert len(film.venue) == 500


@pytest.mark.asyncio
async def test_an_over_long_event_link_rejects_only_that_event(client, db_session, site):
    source = Source(JAZZ, {**SLAM, "url": "https://example.com/events/" + "s" * 2100})
    result = await check(db_session, site, source)

    assert result.status in ("success", "partial")
    assert [e.title for e in db_session.query(Event)] == ["Jazz Night"]
