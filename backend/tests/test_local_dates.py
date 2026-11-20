"""Event dates stay on the town's own calendar, whatever UTC's date is.

Regression tests for the "today shows tomorrow's events" bug: "today" was
computed in UTC, and importers stored a UTC instant's clock time as if it were
the town's, so an 8 PM show in Bloomington became 1 AM the next day.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time

import pytest

from app.core.formatting import time_zone_abbreviation
from app.core.timezones import local_date, resolve_zone_name
from app.extraction.normalize import normalize_candidate
from app.extraction.patterns.ics_calendar import IcsCalendarPattern
from app.extraction.types import EventCandidate
from app.repositories import public_events
from app.repositories.public_events import (
    LocalDates,
    list_public_events,
    local_today,
    this_weekend,
    town_today,
    upcoming_counts_by_city,
)
from app.schemas.extraction import SiteConfiguration
from tests.extraction_helpers import make_response

INDY = "America/Indiana/Indianapolis"
LA = "America/Los_Angeles"

# Thursday Nov 19 2026, 8:30 PM in Bloomington; UTC is already on Friday.
EVENING_IN_BLOOMINGTON = datetime(2026, 11, 20, 1, 30, tzinfo=UTC)


def _candidate(**raw) -> EventCandidate:
    return EventCandidate(
        raw={"title": "Show", "canonical_url": "https://example.com/e/1", **raw},
        title=None, canonical_url=None, description=None, start_date=None,
        start_time=None, end_date=None, end_time=None, timezone=None, venue=None,
        address=None, image_url=None, latitude=None, longitude=None,
        source_category=None, external_source_id=None, field_source_paths={},
        transformation_history=(), source_page="https://example.com/events",
        extraction_pattern="json_ld_event", warnings=(), raw_record_hash="x",
    )


def _config(timezone: str | None = None) -> SiteConfiguration:
    return SiteConfiguration(
        pattern_name="json_ld_event", listing_url="https://example.com/events",
        timezone=timezone,
    )


def _normalized(start, *, timezone=None, fallback=INDY, **raw):
    return normalize_candidate(
        _candidate(start_datetime=start, **raw), _config(timezone), fallback_timezone=fallback
    )


# --- Zone resolution ---------------------------------------------------------


def test_bare_abbreviations_resolve_to_a_dst_aware_region():
    assert resolve_zone_name("EST") == "America/New_York"
    assert resolve_zone_name("PST") == LA
    # A town's proper zone wins over a source's bare abbreviation.
    assert resolve_zone_name("EST", INDY) == INDY
    assert resolve_zone_name(INDY, "EST") == INDY
    assert resolve_zone_name(None, INDY) == INDY
    assert resolve_zone_name("Not/A_Zone") is None


def test_a_july_event_in_an_est_town_is_labelled_edt():
    assert time_zone_abbreviation("EST", date(2026, 7, 4), time(20, 0)) == "EDT"
    assert time_zone_abbreviation("EST", date(2026, 12, 4), time(20, 0)) == "EST"


def test_local_date_differs_from_utc_in_the_evening():
    assert EVENING_IN_BLOOMINGTON.date() == date(2026, 11, 20)
    assert local_date(INDY, EVENING_IN_BLOOMINGTON) == date(2026, 11, 19)
    assert local_date(LA, EVENING_IN_BLOOMINGTON) == date(2026, 11, 19)
    assert local_date("UTC", EVENING_IN_BLOOMINGTON) == date(2026, 11, 20)


# --- Scraped instants land on the local wall clock -----------------------------


@pytest.mark.parametrize(
    ("start", "expected_date", "expected_time"),
    [
        # 8 PM EST is 01:00 UTC the next day.
        ("2026-11-20T01:00:00Z", "2026-11-19", "20:00:00"),
        ("2026-11-20T01:00:00+00:00", "2026-11-19", "20:00:00"),
        # Summer: 8:30 PM EDT is 00:30 UTC.
        ("2026-07-04T00:30:00Z", "2026-07-03", "20:30:00"),
        # Around midnight: 11:59 PM local stays on its own day...
        ("2026-11-20T04:59:00Z", "2026-11-19", "23:59:00"),
        # ...and 12:01 AM is the next day.
        ("2026-11-20T05:01:00Z", "2026-11-20", "00:01:00"),
        # An offset already in the town's zone is kept as written.
        ("2026-11-19T20:00:00-05:00", "2026-11-19", "20:00:00"),
    ],
)
def test_utc_and_offset_times_become_bloomington_wall_clock(start, expected_date, expected_time):
    event = _normalized(start)
    assert event.start_date.isoformat() == expected_date
    assert event.start_time.isoformat() == expected_time
    assert event.start_time.tzinfo is None
    assert event.timezone == INDY


def test_daylight_saving_transitions():
    # Spring forward (Mar 8 2026, 2 AM -> 3 AM): 07:30 UTC is 3:30 AM EDT.
    spring = _normalized("2026-03-08T07:30:00Z")
    assert (spring.start_date.isoformat(), spring.start_time.isoformat()) == (
        "2026-03-08", "03:30:00"
    )
    # Fall back (Nov 1 2026): 1:30 AM happens twice, once in EDT and once in EST.
    first = _normalized("2026-11-01T05:30:00Z")
    second = _normalized("2026-11-01T06:30:00Z")
    assert first.start_time.isoformat() == second.start_time.isoformat() == "01:30:00"
    assert first.start_date == second.start_date == date(2026, 11, 1)


def test_a_bare_est_source_setting_still_applies_summer_time():
    # 00:00 UTC on July 4 is 8 PM EDT on July 3, not 7 PM.
    event = _normalized("2026-07-04T00:00:00Z", timezone="EST", fallback="EST")
    assert event.start_time.isoformat() == "20:00:00"
    assert event.start_date.isoformat() == "2026-07-03"


def test_date_only_values_are_calendar_dates_not_utc_midnight():
    event = _normalized("2026-11-20")
    assert event.start_date == date(2026, 11, 20)
    assert event.start_time is None


def test_naive_local_times_are_left_alone():
    event = _normalized("2026-11-19T20:00:00")
    assert (event.start_date, event.start_time) == (date(2026, 11, 19), time(20, 0))


def test_the_same_instant_in_different_towns():
    instant = "2026-11-20T01:00:00Z"
    bloomington = _normalized(instant, fallback=INDY)
    berkeley = _normalized(instant, fallback=LA)
    assert (bloomington.start_date, bloomington.start_time) == (date(2026, 11, 19), time(20))
    assert (berkeley.start_date, berkeley.start_time) == (date(2026, 11, 19), time(17))


def test_multi_day_end_converts_with_its_own_date():
    event = _normalized("2026-11-15T22:00:00Z", end_datetime="2026-11-22T04:00:00Z")
    assert (event.start_date, event.start_time) == (date(2026, 11, 15), time(17))
    assert (event.end_date, event.end_time) == (date(2026, 11, 21), time(23))


def test_town_without_a_zone_keeps_the_sources_own_clock():
    # A town still on the UTC default: moving times to UTC would be wrong.
    event = _normalized("2026-11-19T20:00:00-05:00", fallback="UTC")
    assert (event.start_date, event.start_time) == (date(2026, 11, 19), time(20))


def test_ics_utc_times_are_placed_on_the_town_clock():
    feed = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:test\r\n"
        "BEGIN:VEVENT\r\nUID:a@example.org\r\nSUMMARY:Late show\r\n"
        "DTSTART:20261120T010000Z\r\nDTEND:20261120T030000Z\r\n"
        "URL:https://example.org/late\r\nEND:VEVENT\r\n"
        "BEGIN:VEVENT\r\nUID:b@example.org\r\nSUMMARY:Local show\r\n"
        "DTSTART;TZID=America/New_York:20260704T200000\r\n"
        "URL:https://example.org/local\r\nEND:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    config = SiteConfiguration(
        pattern_name="ics_calendar", api_endpoint="https://example.org/events.ics"
    )
    response = make_response(
        feed, final_url="https://example.org/events.ics", content_type="text/calendar"
    )
    late, local = (
        normalize_candidate(c, config, fallback_timezone=INDY)
        for c in IcsCalendarPattern().extract(response, config)
    )
    assert (late.start_date, late.start_time) == (date(2026, 11, 19), time(20))
    assert (late.end_date, late.end_time) == (date(2026, 11, 19), time(22))
    assert (local.start_date, local.start_time) == (date(2026, 7, 4), time(20))


# --- "Today" and "This weekend" are the town's ---------------------------------


@pytest.fixture
def evening(monkeypatch):
    monkeypatch.setattr(public_events, "_utcnow", lambda: EVENING_IN_BLOOMINGTON)
    return EVENING_IN_BLOOMINGTON


def _bloomington(make_city, make_website):
    city = make_city(name="Bloomington", slug="bloomington-in", timezone=INDY)
    site = make_website(
        city, name="IU", is_active=True, approved_pattern={"pattern_name": "livewhale_json"}
    )
    return city, site


def test_town_today_is_the_towns_date(evening, make_city):
    city = make_city(timezone=INDY)
    assert town_today(city) == date(2026, 11, 19)


def test_tonights_event_is_listed_under_today(
    evening, db_session, make_city, make_website, make_event
):
    city, site = _bloomington(make_city, make_website)
    tonight = make_event(city, website=site, title="Tonight", start_date=date(2026, 11, 19),
                         start_time=time(20), canonical_url="https://x/1")
    make_event(city, website=site, title="Tomorrow", start_date=date(2026, 11, 20),
               start_time=time(9), canonical_url="https://x/2")
    today = town_today(city)
    events, total, _ = list_public_events(
        db_session, today=today, city_id=city.id, date_from=today, date_to=today
    )
    assert [e.id for e in events] == [tonight.id]
    assert total == 1


def test_today_page_uses_the_towns_date(
    evening, client, make_city, make_website, make_event
):
    city, site = _bloomington(make_city, make_website)
    make_event(city, website=site, title="Recital Tonight", start_date=date(2026, 11, 19),
               start_time=time(20), canonical_url="https://x/1")
    make_event(city, website=site, title="Saturday Market", start_date=date(2026, 11, 21),
               canonical_url="https://x/2")
    today_page = client.get("/city/bloomington-in/today").text
    assert "Recital Tonight" in today_page
    assert "Saturday Market" not in today_page
    weekend_page = client.get("/city/bloomington-in/this-weekend").text
    assert "Saturday Market" in weekend_page
    assert "Recital Tonight" not in weekend_page


def test_multi_day_event_is_in_today_while_it_runs(
    evening, db_session, make_city, make_website, make_event
):
    city, site = _bloomington(make_city, make_website)
    running = make_event(city, website=site, title="Exhibit", start_date=date(2026, 11, 1),
                         end_date=date(2026, 12, 6), canonical_url="https://x/1")
    make_event(city, website=site, title="Ended", start_date=date(2026, 11, 1),
               end_date=date(2026, 11, 18), canonical_url="https://x/2")
    today = town_today(city)
    events, _, _ = list_public_events(
        db_session, today=today, city_id=city.id, date_from=today, date_to=today
    )
    assert [e.id for e in events] == [running.id]


def test_upcoming_counts_use_each_towns_own_date(
    evening, db_session, make_city, make_website, make_event
):
    # At 01:30 UTC an event that ended "yesterday" by UTC is still tonight in
    # both towns; one dated the UTC day is upcoming everywhere.
    bloomington, b_site = _bloomington(make_city, make_website)
    utc_town = make_city(name="Nowhere", slug="nowhere", timezone="UTC")
    u_site = make_website(
        utc_town, name="U", is_active=True, approved_pattern={"pattern_name": "x"}
    )
    for city, site, url in ((bloomington, b_site, "https://b/1"), (utc_town, u_site, "https://u/1")):
        make_event(city, website=site, title="Thursday night", start_date=date(2026, 11, 19),
                   canonical_url=url)
    counts = upcoming_counts_by_city(db_session, today=local_today(db_session))
    assert counts.get(bloomington.id) == 1
    assert counts.get(utc_town.id) is None  # Thursday is over in a UTC town


def test_weekend_per_zone():
    # Friday evening in Los Angeles is already Saturday in UTC.
    dates = LocalDates({LA: date(2026, 11, 20), "UTC": date(2026, 11, 21)}, date(2026, 11, 21))
    saturday, sunday = this_weekend(dates)
    assert saturday.for_zone(LA) == date(2026, 11, 21)
    assert sunday.for_zone(LA) == date(2026, 11, 22)
    assert saturday.for_zone("UTC") == date(2026, 11, 21)
    assert this_weekend(date(2026, 11, 22)) == (date(2026, 11, 21), date(2026, 11, 22))


def test_listing_is_in_time_order_within_a_day(
    evening, db_session, make_city, make_website, make_event
):
    city, site = _bloomington(make_city, make_website)
    late = make_event(city, website=site, title="Late", start_date=date(2026, 11, 21),
                      start_time=time(21), canonical_url="https://x/1")
    untimed = make_event(city, website=site, title="All day", start_date=date(2026, 11, 21),
                         canonical_url="https://x/2")
    early = make_event(city, website=site, title="Early", start_date=date(2026, 11, 21),
                       start_time=time(9), canonical_url="https://x/3")
    events, _, _ = list_public_events(db_session, today=town_today(city), city_id=city.id)
    assert [e.id for e in events] == [early.id, late.id, untimed.id]
