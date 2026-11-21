"""Reading a paged feed to its end, and saying so when a page limit stops it.

IU's LiveWhale feed has 15 pages, but imports asked for pages 16 to 25 too,
because numbered query-parameter pagination never stopped on an empty page.
And had the feed grown past the page limit, the walk would have ended there
with no sign anything was left unread. An import that stops at a limit must
say so, must not count what it never reached as missing, and must never take
an event down because it was not reached.
"""

from __future__ import annotations

import json
from datetime import date

import httpx
import pytest

from app.core.permissions import ADMINISTRATOR
from app.models.notification import Notification
from app.schemas.extraction import SiteConfiguration
from app.services.extraction_runs import listing_incomplete, run_extraction
from app.services.fingerprints import update_fingerprint_and_duplicates
from tests.extraction_helpers import patched_http_fetch

INDY = "America/Indiana/Indianapolis"
FEED = "https://events.iu.edu/live/json/events"


def _record(event_id: int, day: str) -> dict:
    return {
        "id": event_id, "title": f"Talk {event_id}",
        "url": f"https://events.iu.edu/x/event/{event_id}-talk",
        "date_iso": f"{day}T18:00:00-05:00", "date2_iso": f"{day}T19:00:00-05:00",
        "timezone": INDY, "location_title": "Wells Library",
    }


PAGES = {
    "1": [_record(1, "2027-01-04"), _record(2, "2027-01-05")],
    "2": [_record(3, "2027-01-06"), _record(4, "2027-01-07")],
    "3": [_record(5, "2027-01-08")],
}


def _handler(pages: dict[str, list[dict]], requested: list[str], *, total_pages=None):
    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page", "1")
        if "/live/json/events" not in request.url.path:
            # An event's own page, looked up for a time; not a feed page.
            return httpx.Response(404, text="")
        requested.append(page)
        body: dict = {"data": pages.get(page, [])}
        if total_pages is not None:
            body["meta"] = {"page": int(page), "total_pages": total_pages, "per_page": 2}
        return httpx.Response(200, text=json.dumps(body),
                              headers={"content-type": "application/json"})

    return handler


@pytest.fixture
def feed_source(db_session, make_city, make_website, make_user):
    make_user(email="approver@example.com", role_name=ADMINISTRATOR)
    city = make_city(name="Bloomington", slug="bloomington-in", timezone=INDY)
    website = make_website(city, name="IU", base_url="https://events.iu.edu", is_active=True)
    website.onboarding_status = "active"
    db_session.commit()

    def configure(max_pages: int):
        website.approved_pattern = SiteConfiguration(
            pattern_name="livewhale_json", api_endpoint=FEED,
            pagination={"strategy": "query_param", "page_param": "page",
                        "max_pages": max_pages},
            fetch={"rate_limit_delay_seconds": 0.0},
        ).model_dump(mode="json")
        db_session.commit()

    return city, website, configure


def _cap_notices(db_session) -> int:
    return db_session.query(Notification).filter_by(
        notification_type="extraction_page_cap_reached"
    ).count()


@pytest.mark.asyncio
async def test_walk_stops_at_the_empty_page_after_the_last(db_session, feed_source):
    _, website, configure = feed_source
    configure(max_pages=25)
    requested: list[str] = []
    with patched_http_fetch(_handler(PAGES, requested)):
        result = await run_extraction(db_session, website, triggered_by_user_id=None)

    assert requested == ["1", "2", "3", "4"]  # page 4 is empty: the feed has ended
    assert result.events_inserted == 5
    assert not listing_incomplete(result.warnings)
    assert _cap_notices(db_session) == 0


@pytest.mark.asyncio
async def test_walk_stops_at_the_page_count_the_feed_states(db_session, feed_source):
    _, website, configure = feed_source
    configure(max_pages=25)
    requested: list[str] = []
    with patched_http_fetch(_handler(PAGES, requested, total_pages=3)):
        result = await run_extraction(db_session, website, triggered_by_user_id=None)

    assert requested == ["1", "2", "3"]
    assert not listing_incomplete(result.warnings)


@pytest.mark.asyncio
async def test_a_limit_equal_to_the_last_page_is_not_a_truncation(db_session, feed_source):
    _, website, configure = feed_source
    configure(max_pages=3)
    requested: list[str] = []
    with patched_http_fetch(_handler(PAGES, requested, total_pages=3)):
        result = await run_extraction(db_session, website, triggered_by_user_id=None)

    assert requested == ["1", "2", "3"]
    assert not listing_incomplete(result.warnings)
    assert _cap_notices(db_session) == 0


@pytest.mark.asyncio
async def test_stopping_at_the_page_limit_warns_and_removes_nothing(
    db_session, feed_source, make_event
):
    city, website, configure = feed_source
    configure(max_pages=2)
    # Stored by an earlier, complete import; listed on page 3, past the limit.
    beyond = make_event(
        city, website=website, title="Talk 5", external_source_id="5",
        canonical_url="https://events.iu.edu/x/event/5-talk",
        start_date=date(2027, 1, 8),
    )
    update_fingerprint_and_duplicates(db_session, beyond)
    db_session.commit()

    requested: list[str] = []
    with patched_http_fetch(_handler(PAGES, requested)):
        result = await run_extraction(db_session, website, triggered_by_user_id=None)

    assert requested == ["1", "2"]
    assert "pagination_max_pages_reached:2" in result.warnings
    assert listing_incomplete(result.warnings)
    assert result.status == "success"  # the pages it did read still count
    assert result.events_inserted == 4
    # Nothing past the limit is counted as gone, and nothing is taken down.
    assert result.events_missing == 0
    db_session.refresh(beyond)
    assert beyond.is_active and beyond.archived_at is None
    assert _cap_notices(db_session) == 1


@pytest.mark.asyncio
async def test_a_complete_read_still_counts_but_never_hides_absent_events(
    db_session, feed_source, make_event
):
    city, website, configure = feed_source
    configure(max_pages=25)
    gone = make_event(
        city, website=website, title="Talk 9", external_source_id="9",
        canonical_url="https://events.iu.edu/x/event/9-talk",
        start_date=date(2027, 1, 9),
    )
    update_fingerprint_and_duplicates(db_session, gone)
    db_session.commit()

    with patched_http_fetch(_handler(PAGES, [])):
        result = await run_extraction(db_session, website, triggered_by_user_id=None)

    assert result.events_missing == 1
    db_session.refresh(gone)
    assert gone.is_active


@pytest.mark.asyncio
async def test_the_run_page_says_the_listing_was_not_read_to_the_end(
    db_session, feed_source, client, make_super_admin, login
):
    _, website, configure = feed_source
    configure(max_pages=2)
    with patched_http_fetch(_handler(PAGES, [])):
        result = await run_extraction(db_session, website, triggered_by_user_id=None)
    make_super_admin(email="runs@example.com", password="x")
    login("runs@example.com", "x")

    body = client.get(f"/admin/websites/{website.id}/runs/{result.run_id}").text
    assert "Listing read to the end" in body
    assert "not counted (incomplete run)" in body
    assert "pagination_max_pages_reached:2" in body


@pytest.mark.asyncio
async def test_a_record_served_on_two_pages_marks_the_read_incomplete(
    db_session, feed_source, make_event
):
    # The source's page boundaries moved mid-walk: page 2 repeats a page 1
    # record, so another record (Talk 4) was never served. Seen on IU's feed:
    # run #1980 skipped 65 records as duplicates and missed as many sessions.
    city, website, configure = feed_source
    configure(max_pages=25)
    unserved = make_event(
        city, website=website, title="Talk 4", external_source_id="4",
        canonical_url="https://events.iu.edu/x/event/4-talk",
        start_date=date(2027, 1, 7),
    )
    update_fingerprint_and_duplicates(db_session, unserved)
    db_session.commit()
    shifted = {
        "1": [_record(1, "2027-01-04"), _record(2, "2027-01-05")],
        "2": [_record(2, "2027-01-05"), _record(3, "2027-01-06")],
        "3": [_record(5, "2027-01-08")],
    }
    with patched_http_fetch(_handler(shifted, [])):
        result = await run_extraction(db_session, website, triggered_by_user_id=None)

    assert "pagination_overlap:1_records_on_two_pages" in result.warnings
    assert listing_incomplete(result.warnings)
    assert result.events_missing == 0
    db_session.refresh(unserved)
    assert unserved.is_active
