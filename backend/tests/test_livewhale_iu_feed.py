"""IU's LiveWhale feed (events.iu.edu/live/json/events): dates, times, venues.

Regression tests for IU events showing "Date TBD / Time TBD / Venue TBD" or a
wrong day: the feed's unix timestamps were read as local clock times, its end
time (`date2_*`) and all-day flag (`is_all_day`) were never read, and the
location, description and image are only returned when asked for.
"""

from __future__ import annotations

import json
from datetime import date, time

import httpx
import pytest

from app.extraction.normalize import ALL_DAY_MARKER, normalize_candidate
from app.extraction.patterns.livewhale_json import (
    RESPONSE_FIELDS,
    LiveWhalePattern,
    request_url,
)
from app.models.event import Event
from app.schemas.extraction import SiteConfiguration
from app.services.extraction_runs import run_extraction
from app.services.fingerprints import update_fingerprint_and_duplicates
from tests.extraction_helpers import load_fixture, make_response, patched_http_fetch

INDY = "America/Indiana/Indianapolis"
FEED = "https://events.iu.edu/live/json/events"
CONFIG = SiteConfiguration(pattern_name="livewhale_json", api_endpoint=FEED)


def _events():
    response = make_response(
        load_fixture("livewhale_iu_events.json"), final_url=FEED,
        content_type="application/json",
    )
    candidates = LiveWhalePattern().extract(response, CONFIG)
    return {
        (c.title, c.start_date.isoformat() if c.start_date else None): c
        for c in (normalize_candidate(x, CONFIG, fallback_timezone=INDY) for x in candidates)
    }


def test_request_url_asks_for_location_description_and_image():
    url = request_url(FEED)
    assert url.startswith(f"{FEED}/response_fields/")
    for field in ("location", "description", "image", "event_types"):
        assert field in url
    # Never doubled, and other endpoints are left alone.
    assert request_url(url) == url
    assert request_url("https://example.edu/calendar/api/1/events") == (
        "https://example.edu/calendar/api/1/events"
    )
    fields = ",".join(RESPONSE_FIELDS)
    assert request_url(f"{FEED}/group/music") == f"{FEED}/response_fields/{fields}/group/music"


def test_afternoon_event_keeps_its_local_time_venue_and_details():
    event = _events()[("Mindfulness at the Museum", "2026-11-19")]
    assert (event.start_time, event.end_date, event.end_time) == (
        time(14), date(2026, 11, 19), time(15)
    )
    assert event.venue == "Sidney and Lois Eskenazi Museum of Art"
    assert event.description and "make some art" in event.description
    assert event.image_url.endswith("mindful.jpg")
    assert event.source_category == "Arts & Culture"
    assert (event.latitude, event.longitude) == (39.1677, -86.5161)
    assert event.canonical_url.startswith("https://events.iu.edu/artmuseum/event/2344735")


def test_eight_pm_event_stays_on_its_own_day():
    # date_ts is 01:00 UTC on Nov 20; the event is Thursday Nov 19 at 8 PM.
    event = _events()[("Evening Recital", "2026-11-19")]
    assert event.start_time == time(20)
    assert event.end_date is None and event.end_time is None
    assert event.venue == "Auer Hall"  # from location_title
    assert event.source_category == "Jacobs School of Music"  # group fallback


def test_all_day_multi_day_event_is_dates_only():
    event = _events()[("Hoosier Pet Care Drive", "2026-11-09")]
    assert event.end_date == date(2026, 12, 11)
    assert event.start_time is None and event.end_time is None
    assert ALL_DAY_MARKER in event.transformation_history


def test_timed_multi_day_event_has_its_real_end():
    event = _events()[("Turkey Madness", "2026-11-15")]
    assert (event.start_time, event.end_date, event.end_time) == (
        time(17), date(2026, 11, 21), time(23)
    )


def test_timestamps_alone_are_converted_to_local_time():
    record = {
        "id": 7, "title": "Late Lecture", "url": "https://events.iu.edu/x/event/7",
        "date_ts": 1795136400, "date2_ts": 1795143600, "timezone": "America/New_York",
    }
    response = make_response(json.dumps({"data": [record]}), final_url=FEED,
                             content_type="application/json")
    (candidate,) = LiveWhalePattern().extract(response, CONFIG)
    event = normalize_candidate(candidate, CONFIG, fallback_timezone=INDY)
    assert (event.start_date, event.start_time) == (date(2026, 11, 19), time(20))
    assert (event.end_date, event.end_time) == (date(2026, 11, 19), time(22))


def test_each_session_of_a_repeating_event_is_its_own_listing():
    events = _events()
    first = events[("Closed for Thanksgiving Holiday", "2026-11-25")]
    second = events[("Closed for Thanksgiving Holiday", "2026-11-26")]
    assert first.external_source_id == "1866784@2026-11-25"
    assert second.external_source_id == "1866784@2026-11-26"
    assert first.recurrence_parent_id == second.recurrence_parent_id == "1866784"
    # Single events keep the bare id they were stored under before.
    assert events[("Evening Recital", "2026-11-19")].external_source_id == "2501001"


def test_cancelled_events_are_flagged():
    record = {"id": 9, "title": "Called off", "url": "https://events.iu.edu/x/event/9",
              "date_iso": "2026-11-21T19:00:00-05:00", "is_canceled": 1}
    response = make_response(json.dumps({"data": [record]}), final_url=FEED,
                             content_type="application/json")
    (candidate,) = LiveWhalePattern().extract(response, CONFIG)
    assert candidate.is_cancelled


# --- Repairing records stored by the old importer -----------------------------


@pytest.fixture
def iu_source(db_session, make_city, make_website):
    city = make_city(name="Bloomington", slug="bloomington-in", timezone=INDY)
    website = make_website(city, name="IU", base_url="https://events.iu.edu",
                           is_active=True, approved_pattern=CONFIG.model_dump(mode="json"))
    website.onboarding_status = "active"
    db_session.commit()
    return city, website


def _feed_handler(seen_urls: list[str]):
    body = load_fixture("livewhale_iu_events.json")

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return httpx.Response(200, text=body, headers={"content-type": "application/json"})

    return handler


@pytest.mark.asyncio
async def test_rerun_repairs_old_records_without_duplicates(
    db_session, iu_source, make_event
):
    city, website = iu_source
    # As the old importer stored them: UTC clock times, the next day, no
    # venue or end, and an all-day drive at "5:00 AM".
    recital = make_event(
        city, website=website, title="Evening Recital", external_source_id="2501001",
        canonical_url="https://events.iu.edu/musiciub/event/2501001-evening-recital",
        start_date=date(2026, 11, 20), start_time=time(1), timezone="EST",
    )
    drive = make_event(
        city, website=website, title="Hoosier Pet Care Drive", external_source_id="2457661",
        canonical_url="https://events.iu.edu/educationiub/event/2457661-hoosier-pet-care-drive",
        start_date=date(2026, 11, 16), start_time=time(5), timezone="EST",
    )
    # A repeating series stored as one row under its bare id.
    series = make_event(
        city, website=website, title="Closed for Thanksgiving Holiday",
        external_source_id="1866784", start_date=date(2026, 11, 25), start_time=time(5),
        canonical_url="https://events.iu.edu/iukartgallery/event/1866784-closed",
    )
    # A curator's venue correction must survive.
    recital.corrected_venue = "Auer Hall (Simon Center)"
    for row in (recital, drive, series):
        update_fingerprint_and_duplicates(db_session, row)

    seen: list[str] = []
    with patched_http_fetch(_feed_handler(seen)):
        result = await run_extraction(db_session, website, triggered_by_user_id=None)

    assert result.status == "success"
    assert seen and "/response_fields/" in seen[0]
    for row in (recital, drive, series):
        db_session.refresh(row)

    assert (recital.start_date, recital.start_time) == (date(2026, 11, 19), time(20))
    assert recital.venue == "Auer Hall"
    assert recital.corrected_venue == "Auer Hall (Simon Center)"
    assert recital.timezone == INDY

    assert (drive.start_date, drive.end_date) == (date(2026, 11, 9), date(2026, 12, 11))
    assert drive.start_time is None and drive.end_time is None
    assert drive.venue == "School of Education"

    # The old series row became the first session; the second is new.
    assert series.external_source_id == "1866784@2026-11-25"
    assert series.start_time is None
    sessions = db_session.query(Event).filter(
        Event.title == "Closed for Thanksgiving Holiday"
    ).order_by(Event.start_date).all()
    assert [s.start_date for s in sessions] == [date(2026, 11, 25), date(2026, 11, 26)]

    # Six records, three already stored: three inserted, nothing duplicated.
    assert result.events_inserted == 3
    assert db_session.query(Event).filter(Event.website_id == website.id).count() == 6

    # Running again changes nothing.
    with patched_http_fetch(_feed_handler([])):
        again = await run_extraction(db_session, website, triggered_by_user_id=None)
    assert again.events_inserted == 0
    assert db_session.query(Event).filter(Event.website_id == website.id).count() == 6
