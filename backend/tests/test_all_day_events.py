"""All-day events read "All day", unknown times read "Time TBD", and timed
events keep their times.

Production showed a library exhibit as "12:00 AM to 11:59 PM EST" and told
Google it ran 00:00 to 23:59 at -05:00. The library's calendar API sends that
span together with the display text "All day". Bulletin kept the span and
dropped the text. Date-only events, which some sources use for all-day events,
read "Time TBD" because no flag told the two apart.

The fixture records below are sanitized and modeled on the library calendar's
record shape (a bare JSON array read by the simpleview_events pattern with the
same field paths and transformations as the production configuration). They
are not captured bodies.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime, time, timedelta

import pytest
from sqlalchemy import update

from app.extraction.normalize import ALL_DAY_MARKER, normalize_candidate
from app.extraction.patterns.simpleview_events import NAME, SimpleviewEventsPattern
from app.models.event import Event
from app.models.event_provenance import EventProvenance
from app.models.website import Website
from app.repositories.event import create_event_from_candidate, update_event
from app.repositories.public_events import current_public_date
from app.schemas.extraction import SiteConfiguration, TransformationRuleConfig
from app.services import seo
from app.services.all_day_backfill import (
    SKIP_ALREADY_ALL_DAY,
    SKIP_ARCHIVED,
    SKIP_CHANGED,
    SKIP_CHANGED_DURING_APPLY,
    SKIP_DUPLICATE_ID,
    SKIP_HAS_TIME,
    SKIP_NOT_FLAGGED,
    SKIP_NOT_FOUND,
    SKIP_TIME_TBD_SOURCE,
    SKIP_UNVERIFIED,
    CheckRejected,
    apply_all_day_to_ids,
    backfill_all_day,
    load_check_for_apply,
    parse_event_ids,
)
from app.services.calendar_feed import town_calendar

from .extraction_helpers import make_response

ENDPOINT = "https://library.example.org/eeventcaldata"
TZ = "America/Indiana/Indianapolis"
LIBRARY_CONFIG = SiteConfiguration(
    pattern_name=NAME,
    api_endpoint=ENDPOINT,
    timezone=TZ,
    json_paths={
        "events_root": "",
        "start_datetime": "event_start",
        "end_datetime": "event_end",
        "venue": "location",
        "canonical_url": "url",
    },
    transformations=[
        TransformationRuleConfig(
            field="start_datetime", kind="literal_replace", params={"find": " ", "replace": "T"}
        ),
        TransformationRuleConfig(
            field="end_datetime", kind="literal_replace", params={"find": " ", "replace": "T"}
        ),
    ],
)


def _library_record(**values) -> dict:
    record = {
        "id": "1001",
        "title": "Stops Along the Way - Recent Paintings",
        "description": "Hosted by the artist",
        "event_start": "2026-11-22 00:00:00",
        "event_end": "2026-11-22 23:59:00",
        "time_string": "All day",
        "start_time": "12:00am",
        "end_time": "11:59pm",
        "location": "Downtown Library",
        "url": "https://library.example.org/event/1001",
        "tags": "Exhibits",
    }
    record.update(values)
    return record


def _library_candidates(*records):
    response = make_response(
        json.dumps(list(records)), final_url=ENDPOINT, content_type="application/json"
    )
    return [
        normalize_candidate(c, LIBRARY_CONFIG, fallback_timezone=TZ)
        for c in SimpleviewEventsPattern().extract(response, LIBRARY_CONFIG)
    ]


def _event_data(html: str) -> dict:
    for block in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S):
        data = json.loads(block)
        if data.get("@type") == "Event":
            return data
    raise AssertionError("no Event JSON-LD on the page")


@pytest.fixture
def town(db_session, make_city, make_website):
    city = make_city(name="Bloomington", slug="bloomington-in", state_or_region="IN", timezone=TZ)
    website = make_website(city, name="Library")
    website.is_active = True
    website.active_configuration_version = 1
    db_session.commit()
    return city, website


# --- extraction: what counts as all-day -----------------------------------------


def test_library_all_day_text_with_full_day_span_is_all_day():
    [candidate] = _library_candidates(_library_record())
    assert ALL_DAY_MARKER in candidate.transformation_history
    assert candidate.start_date == date(2026, 11, 22)
    assert candidate.start_time is None
    assert candidate.end_time is None
    assert candidate.field_source_paths["all_day"].endswith(".time_string")


def test_library_full_day_span_without_all_day_text_keeps_its_times():
    """00:00 to 23:59 alone is not enough evidence."""
    [candidate] = _library_candidates(_library_record(time_string="12:00am - 11:59pm"))
    assert ALL_DAY_MARKER not in candidate.transformation_history
    assert candidate.start_time == time(0)
    assert candidate.end_time == time(23, 59)


def test_all_day_text_never_overrides_a_real_clock_time():
    [candidate] = _library_candidates(
        _library_record(
            event_start="2026-11-22 09:00:00", event_end="2026-11-22 17:00:00"
        )
    )
    assert ALL_DAY_MARKER not in candidate.transformation_history
    assert (candidate.start_time, candidate.end_time) == (time(9), time(17))


def test_all_day_text_spanning_several_days_keeps_its_times():
    [candidate] = _library_candidates(_library_record(event_end="2026-11-24 23:59:00"))
    assert ALL_DAY_MARKER not in candidate.transformation_history


@pytest.mark.parametrize(
    ("start", "end", "text"),
    [
        ("2026-12-06 00:00:00", "2026-12-06 01:00:00", "12:00am - 1:00am"),  # a midnight formal
        ("2026-12-06 00:00:00", "2026-12-06 00:00:00", "12:00am"),  # a bare midnight start
        ("2026-12-05 22:00:00", "2026-12-06 02:00:00", "10:00pm - 2:00am"),  # crosses midnight
    ],
)
def test_genuine_midnight_events_keep_their_times(start, end, text):
    [candidate] = _library_candidates(
        _library_record(event_start=start, event_end=end, time_string=text)
    )
    assert ALL_DAY_MARKER not in candidate.transformation_history
    assert candidate.start_time is not None


def test_a_timed_library_event_is_untouched():
    [candidate] = _library_candidates(
        _library_record(
            event_start="2026-11-22 13:30:00",
            event_end="2026-11-22 15:00:00",
            time_string="1:30pm - 3:00pm",
        )
    )
    assert ALL_DAY_MARKER not in candidate.transformation_history
    assert (candidate.start_time, candidate.end_time) == (time(13, 30), time(15))


# --- storing: create, update, and what is preserved ---------------------------


def test_import_creates_an_all_day_event(db_session, town):
    city, website = town
    [candidate] = _library_candidates(_library_record())
    event = create_event_from_candidate(
        db_session, candidate, source="Library", city_id=city.id, website_id=website.id
    )
    assert event.all_day is True
    assert event.start_time is None and event.end_time is None


def test_reimport_fixes_a_stored_midnight_to_1159_row_and_keeps_everything_else(
    db_session, town, make_event, make_category
):
    """The 22 production library rows: stored as 00:00 to 23:59 before this
    change. The next import makes them all-day and changes nothing else."""
    city, website = town
    category = make_category(name="Test Arts", slug="test-arts")
    override = make_category(name="Test Community", slug="test-community")
    event = make_event(
        city,
        website=website,
        title="Stops Along the Way - Recent Paintings",
        canonical_url="https://library.example.org/event/1001",
        external_source_id="1001",
        start_date=date(2026, 11, 22),
        end_date=date(2026, 11, 22),
        start_time=time(0),
        end_time=time(23, 59),
        timezone=TZ,
        venue="Downtown Library",
        category=category,
        category_override_id=override.id,
        corrected_venue="Downtown Library Gallery",
        is_active=False,
        visibility_set_by_admin_at=datetime(2026, 11, 20, tzinfo=UTC),
    )
    event_id = event.id

    [candidate] = _library_candidates(_library_record())
    update_event(db_session, event, candidate)

    assert event.id == event_id
    assert event.canonical_url == "https://library.example.org/event/1001"
    assert event.all_day is True
    assert event.start_time is None and event.end_time is None
    assert event.start_date == date(2026, 11, 22)
    assert event.category_override_id == override.id
    assert event.corrected_venue == "Downtown Library Gallery"
    assert event.is_active is False


def test_a_later_clock_time_from_the_source_ends_all_day(db_session, town, make_event):
    city, website = town
    event = make_event(
        city, website=website, start_date=date(2026, 11, 22), all_day=True, timezone=TZ
    )
    [candidate] = _library_candidates(
        _library_record(
            event_start="2026-11-22 13:30:00",
            event_end="2026-11-22 15:00:00",
            time_string="1:30pm - 3:00pm",
        )
    )
    update_event(db_session, event, candidate)
    assert event.all_day is False
    assert event.start_time == time(13, 30)


def test_a_bare_midnight_rescrape_does_not_undo_all_day(db_session, town, make_event):
    """Many listings write "no time given" as a lone 12:00 AM."""
    city, website = town
    event = make_event(
        city, website=website, start_date=date(2026, 11, 22), all_day=True, timezone=TZ
    )
    [candidate] = _library_candidates(
        _library_record(event_end=None, time_string=None, event_start="2026-11-22 00:00:00")
    )
    assert candidate.start_time == time(0) and candidate.end_time is None
    update_event(db_session, event, candidate)
    assert event.all_day is True
    assert event.start_time is None


def test_a_full_day_span_rescrape_without_the_wording_keeps_all_day(
    db_session, town, make_event
):
    """00:00 to 23:59 with no "All day" text is a placeholder for an event the
    source already called all-day, not a clock time."""
    city, website = town
    event = make_event(
        city, website=website, start_date=date(2026, 11, 22), all_day=True, timezone=TZ
    )
    [candidate] = _library_candidates(_library_record(time_string="12:00am - 11:59pm"))
    assert (candidate.start_time, candidate.end_time) == (time(0), time(23, 59))
    update_event(db_session, event, candidate)
    assert event.all_day is True
    assert event.start_time is None and event.end_time is None


# --- what visitors see ----------------------------------------------------------


def _today_in_town():
    return current_public_date()


def test_cards_show_all_day_time_tbd_and_real_times(client, db_session, town, make_event):
    city, website = town
    day = _today_in_town() + timedelta(days=2)
    make_event(
        city, website=website, title="Gallery Show", canonical_url="https://e.org/1",
        start_date=day, all_day=True, timezone=TZ, venue="Downtown Library",
    )
    make_event(
        city, website=website, title="Mystery Time", canonical_url="https://e.org/2",
        start_date=day, timezone=TZ,
    )
    make_event(
        city, website=website, title="Midnight Formal", canonical_url="https://e.org/3",
        start_date=day, start_time=time(0), end_time=time(1), timezone=TZ,
    )

    html = " ".join(client.get(f"/city/{city.slug}").text.split())

    def card(title):
        start = html.index(f"<h3>{title}</h3>")
        return html[start : html.index("event-card-place", start)]

    assert "All day" in card("Gallery Show")
    assert "12:00 AM" not in card("Gallery Show")
    assert "event-tz" not in card("Gallery Show")
    assert "Time TBD" in card("Mystery Time")
    assert "All day" not in card("Mystery Time")
    assert "12:00 AM" in card("Midnight Formal")


def test_multi_day_all_day_card_says_until(client, db_session, town, make_event):
    city, website = town
    day = _today_in_town() + timedelta(days=2)
    make_event(
        city, website=website, title="Book Sale", canonical_url="https://e.org/4",
        start_date=day, end_date=day + timedelta(days=2), all_day=True, timezone=TZ,
    )
    html = " ".join(client.get(f"/city/{city.slug}").text.split())
    start = html.index("<h3>Book Sale</h3>")
    assert "All day, until" in html[start : start + 1500]


def test_detail_page_says_all_day_and_points_to_venue_hours(
    client, db_session, town, make_event
):
    city, website = town
    day = _today_in_town() + timedelta(days=2)
    event = make_event(
        city, website=website, title="Gallery Show", canonical_url="https://e.org/show",
        start_date=day, all_day=True, timezone=TZ, venue="Downtown Library",
    )
    html = client.get(f"/events/{event.id}").text
    when = html[html.index('class="event-detail-datetime"') : html.index("event-detail-venue")]
    assert "All day" in when
    assert "12:00 AM" not in when and "11:59 PM" not in when
    assert "event-tz" not in when
    link = re.search(r'<a class="event-venue-hours[^>]*>', when).group(0)
    assert 'href="https://e.org/show"' in link
    assert 'rel="noopener noreferrer"' in link and 'target="_blank"' in link
    assert "Check venue hours" in when
    # No hours are stated anywhere.
    assert not re.search(r"\d{1,2}:\d\d [AP]M", when)


def test_no_venue_hours_link_without_a_venue_or_for_timed_events(
    client, db_session, town, make_event
):
    city, website = town
    day = _today_in_town() + timedelta(days=2)
    holiday = make_event(
        city, website=website, title="Holiday", canonical_url="https://e.org/h",
        start_date=day, all_day=True, timezone=TZ,
    )
    talk = make_event(
        city, website=website, title="Talk", canonical_url="https://e.org/t",
        start_date=day, start_time=time(18), timezone=TZ, venue="Downtown Library",
    )
    assert "Check venue hours" not in client.get(f"/events/{holiday.id}").text
    talk_html = client.get(f"/events/{talk.id}").text
    assert "Check venue hours" not in talk_html
    assert "6:00 PM" in talk_html


def test_saved_events_show_all_day(client, db_session, town, make_event, make_user, login):
    from app.services import engagement

    city, website = town
    user = make_user(email="saver@example.com", password="saver-pass-123")
    login("saver@example.com", "saver-pass-123")
    day = _today_in_town() + timedelta(days=2)
    event = make_event(
        city, website=website, title="Gallery Show", canonical_url="https://e.org/s",
        start_date=day, all_day=True, timezone=TZ,
    )
    engagement.save_event(db_session, user_id=user.id, event_id=event.id)
    html = " ".join(client.get("/account/saved").text.split())
    assert ", All day" in html
    assert "12:00 AM" not in html


# --- Google structured data and calendar downloads ------------------------------


def test_structured_data_uses_dates_for_all_day(db_session, town, make_event):
    city, website = town
    event = make_event(
        city, website=website, start_date=date(2026, 11, 22), all_day=True, timezone=TZ
    )
    data = seo.event_schema(event)
    assert data["startDate"] == "2026-11-22"
    assert "endDate" not in data


def test_structured_data_never_sends_clock_times_for_an_all_day_event(
    db_session, town, make_event
):
    """Even if a stale time were stored, all-day wins."""
    city, website = town
    event = make_event(
        city, website=website, start_date=date(2026, 11, 22), end_date=date(2026, 11, 22),
        start_time=time(0), end_time=time(23, 59), all_day=True, timezone=TZ,
    )
    data = seo.event_schema(event)
    assert data["startDate"] == "2026-11-22"
    assert "T" not in json.dumps({k: data.get(k) for k in ("startDate", "endDate")})
    assert seo.event_datetimes(event)["start"] == "2026-11-22"
    assert seo.event_datetimes(event)["end"] is None


def test_multi_day_all_day_structured_data(db_session, town, make_event):
    city, website = town
    event = make_event(
        city, website=website, start_date=date(2026, 11, 20), end_date=date(2026, 11, 22),
        all_day=True, timezone=TZ,
    )
    data = seo.event_schema(event)
    assert (data["startDate"], data["endDate"]) == ("2026-11-20", "2026-11-22")


def test_dst_change_day_all_day_has_no_offset_and_timed_keeps_the_right_one(
    db_session, make_city, make_event
):
    """2026-11-01 is the end of daylight saving time in New York."""
    city = make_city(name="Princeton", slug="princeton-nj", timezone="America/New_York")
    all_day = make_event(city, start_date=date(2026, 11, 1), all_day=True)
    before = make_event(city, start_date=date(2026, 11, 1), start_time=time(0, 30))
    after = make_event(city, start_date=date(2026, 11, 1), start_time=time(12))
    assert seo.event_schema(all_day)["startDate"] == "2026-11-01"
    assert seo.event_schema(before)["startDate"] == "2026-11-01T00:30:00-04:00"
    assert seo.event_schema(after)["startDate"] == "2026-11-01T12:00:00-05:00"


def test_genuine_midnight_structured_data_keeps_its_time(db_session, town, make_event):
    city, website = town
    event = make_event(
        city, website=website, start_date=date(2026, 12, 6),
        start_time=time(0), end_time=time(1), timezone=TZ,
    )
    data = seo.event_schema(event)
    assert data["startDate"] == "2026-12-06T00:00:00-05:00"
    assert data["endDate"] == "2026-12-06T01:00:00-05:00"


def _vevents(feed: bytes) -> list[str]:
    text = feed.decode().replace("\r\n ", "")
    return re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", text, re.S)


def test_calendar_download_uses_dates_for_all_day(db_session, town, make_event):
    city, website = town
    single = make_event(
        city, website=website, title="One", start_date=date(2026, 11, 22),
        start_time=time(0), end_time=time(23, 59), all_day=True, timezone=TZ,
    )
    multi = make_event(
        city, website=website, title="Three", canonical_url="https://e.org/m",
        start_date=date(2026, 11, 20), end_date=date(2026, 11, 22), all_day=True, timezone=TZ,
    )
    one, three = _vevents(town_calendar(city, [single, multi]))
    assert "DTSTART;VALUE=DATE:20261122" in one
    assert "T000000" not in one and "T235900" not in one
    assert "DTSTART;VALUE=DATE:20261120" in three
    assert "DTEND;VALUE=DATE:20261123" in three  # iCalendar end dates are exclusive


def test_calendar_download_keeps_cross_midnight_events(db_session, town, make_event):
    city, website = town
    event = make_event(
        city, website=website, title="Late", start_date=date(2026, 12, 5),
        start_time=time(22), end_time=time(2), timezone=TZ,
    )
    [late] = _vevents(town_calendar(city, [event]))
    assert re.search(r"DTSTART;TZID=America/Indiana/Indianapolis:20261205T220000", late)
    assert re.search(r"DTEND;TZID=America/Indiana/Indianapolis:20261206T020000", late)


# --- backfill from extraction history ------------------------------------------


def _provenance(db_session, event, website, history, *, run_id=None):
    row = EventProvenance(
        event_id=event.id,
        extraction_run_id=run_id,
        website_id=website.id,
        source_page="https://e.org/feed",
        extraction_pattern="ics_calendar",
        pattern_version="1",
        raw_record_hash="r",
        source_response_hash="s",
        transformation_history=history,
    )
    db_session.add(row)
    db_session.commit()
    return row


def test_backfill_dry_run_reports_and_changes_nothing(db_session, town, make_event):
    city, website = town
    flagged = make_event(city, website=website, title="Flagged", start_date=date(2026, 12, 1))
    unknown = make_event(city, website=website, title="Unknown", start_date=date(2026, 12, 1))
    span = make_event(
        city, website=website, title="Span", start_date=date(2026, 12, 1),
        start_time=time(0), end_time=time(23, 59),
    )
    stale = make_event(city, website=website, title="Stale", start_date=date(2026, 12, 1))
    _provenance(db_session, flagged, website, ["start_datetime:iso", ALL_DAY_MARKER])
    _provenance(db_session, unknown, website, ["start_datetime:iso"])
    _provenance(db_session, span, website, ["start_datetime:iso"])
    # Flagged once, but the newest extraction no longer says so.
    _provenance(db_session, stale, website, [ALL_DAY_MARKER])
    _provenance(db_session, stale, website, ["start_datetime:iso"])

    report = backfill_all_day(db_session, apply=False)
    assert report.marked_ids == [flagged.id]
    assert report.total_to_mark == 1
    assert sum(report.date_only_unmarked.values()) == 2  # unknown and stale
    assert report.full_day_span_ids == [span.id]
    for event in (flagged, unknown, span, stale):
        db_session.refresh(event)
        assert event.all_day is False


def test_backfill_apply_sets_only_all_day(db_session, town, make_event, make_category):
    city, website = town
    category = make_category()
    stamp = datetime(2026, 11, 1, tzinfo=UTC)
    event = make_event(
        city, website=website, title="Holiday", start_date=date(2026, 12, 1),
        category=category, content_updated_at=stamp, is_active=False,
    )
    _provenance(db_session, event, website, [ALL_DAY_MARKER])
    before = (event.id, event.canonical_url, event.category_id, event.is_active)

    report = backfill_all_day(db_session, apply=True)
    assert report.applied and report.marked_ids == [event.id]
    db_session.refresh(event)
    assert event.all_day is True
    assert (event.id, event.canonical_url, event.category_id, event.is_active) == before
    assert event.content_updated_at.replace(tzinfo=UTC) == stamp
    assert db_session.query(EventProvenance).filter_by(event_id=event.id).count() == 1
    # A second run has nothing left to do.
    assert backfill_all_day(db_session, apply=True).total_to_mark == 0


def test_admin_backfill_preview_is_read_only_and_apply_needs_csrf(
    client, db_session, town, make_event, make_super_admin, login
):
    city, website = town
    event = make_event(city, website=website, title="Holiday", start_date=date(2026, 12, 1))
    _provenance(db_session, event, website, [ALL_DAY_MARKER])
    make_super_admin()
    login("root@example.com")

    page = client.get("/admin/events/all-day-backfill")
    assert page.status_code == 200
    assert "Preview only, nothing changed" in page.text
    db_session.refresh(event)
    assert event.all_day is False

    assert client.post("/admin/events/all-day-backfill", data={}).status_code in (400, 403, 422)
    db_session.refresh(event)
    assert event.all_day is False

    assert 'action="/admin/events/all-day-backfill"' in page.text
    token = client.cookies.get("csrf_token")
    # Without a reviewed id list and a Check nothing is marked, even by a
    # Super Administrator.
    bare = client.post(
        "/admin/events/all-day-backfill",
        data={"csrf_token": token, "mode": "apply", "reviewed_at": REVIEWED_AT},
    )
    assert bare.status_code == 409
    db_session.refresh(event)
    assert event.all_day is False


def test_admin_backfill_needs_admin(client, db_session, make_user, login):
    make_user(email="viewer@example.com")
    login("viewer@example.com")
    assert client.get("/admin/events/all-day-backfill").status_code in (302, 303, 403)


# --- applying a reviewed list of event ids --------------------------------------

REVIEWED_AT = "2026-11-24T23:20:00+00:00"
REVIEWED = datetime(2026, 11, 24, 23, 20, tzinfo=UTC)


def _flagged(db_session, make_event, city, website, title, **values):
    event = make_event(
        city, website=website, title=title, start_date=values.pop("start_date", date(2026, 12, 1)),
        content_updated_at=values.pop("content_updated_at", datetime(2026, 11, 1, tzinfo=UTC)),
        **values,
    )
    _provenance(db_session, event, website, [ALL_DAY_MARKER])
    return event


def test_id_apply_marks_only_listed_events_and_keeps_everything_else(
    db_session, town, make_event, make_category
):
    city, website = town
    category = make_category()
    override = make_category(name="Override category", slug="override-category")
    listed = _flagged(
        db_session, make_event, city, website, "Listed", category=category,
        category_override_id=override.id, is_active=False, duplicate_status="not_duplicate",
        end_date=date(2026, 12, 3),
    )
    unlisted = _flagged(db_session, make_event, city, website, "Unlisted")
    before = (
        listed.start_date, listed.end_date, listed.start_time, listed.end_time,
        listed.is_active, listed.category_id, listed.category_override_id,
        listed.duplicate_status, listed.canonical_url,
    )

    report = apply_all_day_to_ids(db_session, [listed.id], reviewed_at=REVIEWED, apply=True)
    assert report.applied and report.changed_ids == [listed.id] and report.skipped == []
    db_session.refresh(listed)
    db_session.refresh(unlisted)
    assert listed.all_day is True
    assert unlisted.all_day is False
    assert (
        listed.start_date, listed.end_date, listed.start_time, listed.end_time,
        listed.is_active, listed.category_id, listed.category_override_id,
        listed.duplicate_status, listed.canonical_url,
    ) == before
    assert listed.content_updated_at.replace(tzinfo=UTC) == datetime(2026, 11, 1, tzinfo=UTC)
    assert db_session.query(EventProvenance).filter_by(event_id=listed.id).count() == 1


def test_id_check_changes_nothing(db_session, town, make_event):
    city, website = town
    event = _flagged(db_session, make_event, city, website, "Holiday")
    report = apply_all_day_to_ids(db_session, [event.id], reviewed_at=REVIEWED)
    assert not report.applied and report.changed_ids == [event.id]
    db_session.refresh(event)
    assert event.all_day is False


def test_id_apply_rechecks_and_skips_with_reasons(
    db_session, make_city, make_website, make_event
):
    city = make_city(name="Princeton", slug="princeton-nj", timezone="America/New_York")
    campus = make_website(city, name="Campus", approved_pattern={"configuration": {}})
    athletics = make_website(
        city, name="Athletics",
        approved_pattern={"configuration": {"pattern_name": "ics_calendar",
                                            "api_endpoint": ATHLETICS_URL,
                                            "date_only_means_time_tbd": True}},
    )
    good = _flagged(db_session, make_event, city, campus, "Good")
    archived = _flagged(db_session, make_event, city, campus, "Archived", archived=True)
    timed = _flagged(db_session, make_event, city, campus, "Timed", start_time=time(15))
    already = _flagged(db_session, make_event, city, campus, "Already", all_day=True)
    edited = _flagged(
        db_session, make_event, city, campus, "Edited",
        content_updated_at=datetime(2026, 11, 25, tzinfo=UTC),
    )
    game = _flagged(db_session, make_event, city, athletics, "Game")
    unflagged = make_event(city, website=campus, title="Unflagged", start_date=date(2026, 12, 1))
    _provenance(db_session, unflagged, campus, ["start_datetime:iso"])
    ids = [good.id, archived.id, timed.id, already.id, edited.id, game.id, unflagged.id,
           good.id, 999999]

    report = apply_all_day_to_ids(db_session, ids, reviewed_at=REVIEWED, apply=True)
    assert report.changed_ids == [good.id]
    assert dict(report.skipped) == {
        archived.id: SKIP_ARCHIVED,
        timed.id: SKIP_HAS_TIME,
        already.id: SKIP_ALREADY_ALL_DAY,
        edited.id: SKIP_CHANGED,
        game.id: SKIP_TIME_TBD_SOURCE,
        unflagged.id: SKIP_NOT_FLAGGED,
        good.id: SKIP_DUPLICATE_ID,
        999999: SKIP_NOT_FOUND,
    }
    for event in (archived, timed, edited, game, unflagged):
        db_session.refresh(event)
        assert event.all_day is False


def test_id_apply_fails_closed_without_a_change_date(db_session, town, make_event):
    """An event with no content_updated_at is marked only when updated_at
    proves nothing was written to it since the review."""
    city, website = town
    quiet = _flagged(
        db_session, make_event, city, website, "Quiet", content_updated_at=None,
        updated_at=datetime(2026, 11, 1, tzinfo=UTC),
    )
    touched = _flagged(
        db_session, make_event, city, website, "Touched", content_updated_at=None,
        updated_at=datetime(2026, 11, 25, tzinfo=UTC),
    )
    report = apply_all_day_to_ids(
        db_session, [quiet.id, touched.id], reviewed_at=REVIEWED, apply=True
    )
    assert report.changed_ids == [quiet.id]
    assert report.skipped == [(touched.id, SKIP_UNVERIFIED)]
    db_session.refresh(touched)
    assert touched.all_day is False


def _gain_time(db_session, event, website):
    db_session.execute(update(Event).where(Event.id == event.id).values(start_time=time(19)))


def _remove_marker(db_session, event, website):
    # A newer extraction that no longer flags the event all-day.
    db_session.add(EventProvenance(
        event_id=event.id, website_id=website.id, source_page="https://e.org/feed",
        extraction_pattern="ics_calendar", pattern_version="1", raw_record_hash="r2",
        source_response_hash="s2", transformation_history=["start_datetime:iso"],
    ))
    db_session.flush()


def _marker_edited_in_place(db_session, event, website):
    # Provenance is append-only, but the update does not rely on that.
    db_session.execute(
        update(EventProvenance).where(EventProvenance.event_id == event.id)
        .values(transformation_history=["start_datetime:iso"])
    )


def _source_turns_time_tbd(db_session, event, website):
    db_session.execute(
        update(Website).where(Website.id == website.id)
        .values(approved_pattern={"configuration": {"date_only_means_time_tbd": True}})
    )


def _source_config_changes(db_session, event, website):
    db_session.execute(
        update(Website).where(Website.id == website.id)
        .values(approved_pattern={"configuration": {"pattern_name": "other"}})
    )


def _edited_after_review(db_session, event, website):
    db_session.execute(
        update(Event).where(Event.id == event.id)
        .values(content_updated_at=datetime(2026, 11, 25, tzinfo=UTC))
    )


def _archived(db_session, event, website):
    db_session.execute(
        update(Event).where(Event.id == event.id).values(archived_at=datetime.now(UTC))
    )


def _written_without_change_date(db_session, event, website):
    db_session.execute(
        update(Event).where(Event.id == event.id)
        .values(content_updated_at=None, title="Retitled")
    )


@pytest.mark.parametrize(
    "change",
    [_gain_time, _remove_marker, _marker_edited_in_place, _source_turns_time_tbd,
     _source_config_changes, _edited_after_review, _archived, _written_without_change_date],
)
def test_id_apply_update_repeats_every_rule(db_session, town, make_event, monkeypatch, change):
    """Whatever changes between an event's check and its update, the update
    itself matches nothing and the event is reported, not marked."""
    import app.services.all_day_backfill as backfill

    city, website = town
    db_session.execute(
        update(Website).where(Website.id == website.id)
        .values(approved_pattern={"configuration": {}})
    )
    db_session.commit()
    event = _flagged(db_session, make_event, city, website, "Raced")
    real = backfill._skip_reason

    def check_then_race(db, checked, reviewed_at):
        result = real(db, checked, reviewed_at)
        assert result[0] is None
        change(db, checked, website)
        return result

    monkeypatch.setattr(backfill, "_skip_reason", check_then_race)
    report = apply_all_day_to_ids(db_session, [event.id], reviewed_at=REVIEWED, apply=True)
    assert report.changed_ids == []
    assert report.skipped == [(event.id, SKIP_CHANGED_DURING_APPLY)]
    db_session.expire_all()
    assert db_session.get(Event, event.id).all_day is False


def test_id_apply_update_goes_through_when_nothing_changed(
    db_session, town, make_event, monkeypatch
):
    import app.services.all_day_backfill as backfill

    city, website = town
    event = _flagged(db_session, make_event, city, website, "Steady")
    real = backfill._skip_reason
    monkeypatch.setattr(backfill, "_skip_reason", lambda *args: real(*args))
    report = apply_all_day_to_ids(db_session, [event.id], reviewed_at=REVIEWED, apply=True)
    assert report.changed_ids == [event.id]


def test_parse_event_ids():
    assert parse_event_ids("# reviewed list\n12, 13\n#14 15") == [12, 13, 14, 15]
    with pytest.raises(ValueError):
        parse_event_ids("12, thirteen")


def _open_backfill_page(client):
    client.get("/admin/events/all-day-backfill")
    return client.cookies.get("csrf_token")


def _check(client, token, ids, reviewed_at=REVIEWED_AT):
    return client.post(
        "/admin/events/all-day-backfill",
        data={"csrf_token": token, "mode": "check", "reviewed_at": reviewed_at,
              "event_ids": ids},
    )


def _check_id(response) -> str:
    found = re.search(r'name="check_id" value="(\d+)"', response.text)
    assert found, "the Check result offers no Apply"
    return found.group(1)


def test_admin_id_apply_checks_first_then_applies_and_audits(
    client, db_session, town, make_event, make_super_admin, login
):
    from app.models.audit_log import AuditLog

    city, website = town
    listed = _flagged(db_session, make_event, city, website, "Listed")
    unlisted = _flagged(db_session, make_event, city, website, "Unlisted")
    timed = _flagged(db_session, make_event, city, website, "Timed", start_time=time(9))
    make_super_admin()
    login("root@example.com")
    token = _open_backfill_page(client)

    checked = _check(client, token, f"{listed.id}, {timed.id}")
    assert checked.status_code == 200
    assert "Checked only, nothing changed" in checked.text
    assert 'name="mode" value="apply"' in checked.text
    # Apply carries only the check's id: no editable ids or review time.
    assert 'name="event_ids" value=' not in checked.text
    check_id = _check_id(checked)
    db_session.refresh(listed)
    assert listed.all_day is False
    check = db_session.get(AuditLog, int(check_id))
    assert check.action == "events_all_day_checked_by_id"
    assert json.loads(check.after_state)["would_change_ids"] == [listed.id]

    # Ids and a review time posted with Apply are ignored; the check decides.
    applied = client.post(
        "/admin/events/all-day-backfill",
        data={"csrf_token": token, "mode": "apply", "check_id": check_id,
              "event_ids": str(unlisted.id), "reviewed_at": "2026-11-30T00:00:00+00:00"},
    )
    assert applied.status_code == 200
    assert "Applied" in applied.text
    for event in (listed, unlisted, timed):
        db_session.refresh(event)
    assert (listed.all_day, unlisted.all_day, timed.all_day) == (True, False, False)
    audit = (
        db_session.query(AuditLog).filter_by(action="events_all_day_applied_by_id").one()
    )
    assert (audit.entity_type, audit.entity_id) == ("all_day_check", int(check_id))
    after = json.loads(audit.after_state)
    assert after["check_id"] == int(check_id)
    assert after["reviewed_at"] == REVIEWED_AT
    assert after["changed_ids"] == [listed.id]
    assert after["skipped"] == []

    # A check is used once.
    again = client.post(
        "/admin/events/all-day-backfill",
        data={"csrf_token": token, "mode": "apply", "check_id": check_id},
    )
    assert again.status_code == 409
    assert "already been applied" in again.text


@pytest.mark.parametrize("check_id", ["", "abc", "999999"])
def test_admin_direct_apply_without_a_check_changes_nothing(
    client, db_session, town, make_event, make_super_admin, login, check_id
):
    from app.models.audit_log import AuditLog

    city, website = town
    event = _flagged(db_session, make_event, city, website, "Listed")
    make_super_admin()
    login("root@example.com")
    token = _open_backfill_page(client)
    response = client.post(
        "/admin/events/all-day-backfill",
        data={"csrf_token": token, "mode": "apply", "check_id": check_id,
              "event_ids": str(event.id), "reviewed_at": REVIEWED_AT},
    )
    assert response.status_code == 409
    db_session.refresh(event)
    assert event.all_day is False
    assert db_session.query(AuditLog).filter_by(
        action="events_all_day_applied_by_id").count() == 0


def test_admin_apply_rejects_an_audit_entry_that_is_not_a_check(
    client, db_session, town, make_event, make_super_admin, login
):
    from app.services.audit import record_audit

    city, website = town
    event = _flagged(db_session, make_event, city, website, "Listed")
    admin = make_super_admin()
    other = record_audit(
        db_session, actor_id=admin.id, action="something_else",
        after={"reviewed_at": REVIEWED_AT, "would_change_ids": [event.id]},
    )
    login("root@example.com")
    token = _open_backfill_page(client)
    response = client.post(
        "/admin/events/all-day-backfill",
        data={"csrf_token": token, "mode": "apply", "check_id": str(other.id)},
    )
    assert response.status_code == 409
    db_session.refresh(event)
    assert event.all_day is False


def _recorded_check(db_session, user, ids, *, created_at=None):
    from app.services.audit import record_audit

    entry = record_audit(
        db_session, actor_id=user.id, action="events_all_day_checked_by_id",
        entity_type="all_day_check",
        after={"reviewed_at": REVIEWED_AT, "listed_ids": ids, "would_change_ids": ids,
               "skipped": []},
    )
    if created_at is not None:
        entry.created_at = created_at
        db_session.commit()
    return entry


def test_check_record_must_be_recent_and_your_own(db_session, make_super_admin, make_user):
    from app.core.permissions import SUPER_ADMINISTRATOR

    mine = make_super_admin()
    theirs = make_user(email="other-root@example.com", role_name=SUPER_ADMINISTRATOR)
    now = datetime.now(UTC)
    fresh = _recorded_check(db_session, mine, [1, 2])
    loaded = load_check_for_apply(db_session, fresh.id, user_id=mine.id)
    assert loaded.event_ids == [1, 2] and loaded.reviewed_at == REVIEWED

    with pytest.raises(CheckRejected, match="someone else"):
        load_check_for_apply(db_session, fresh.id, user_id=theirs.id)
    old = _recorded_check(db_session, mine, [1], created_at=now - timedelta(hours=2))
    with pytest.raises(CheckRejected, match="more than an hour"):
        load_check_for_apply(db_session, old.id, user_id=mine.id)
    empty = _recorded_check(db_session, mine, [])
    with pytest.raises(CheckRejected, match="nothing to change"):
        load_check_for_apply(db_session, empty.id, user_id=mine.id)


def test_admin_check_refuses_a_future_review_time(
    client, db_session, town, make_event, make_super_admin, login
):
    from app.models.audit_log import AuditLog

    city, website = town
    event = _flagged(db_session, make_event, city, website, "Listed")
    make_super_admin()
    login("root@example.com")
    token = _open_backfill_page(client)
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    response = _check(client, token, str(event.id), reviewed_at=future)
    assert response.status_code >= 400
    assert 'name="check_id"' not in response.text
    assert db_session.query(AuditLog).filter_by(
        action="events_all_day_checked_by_id").count() == 0


def test_admin_id_apply_needs_super_administrator(
    client, db_session, town, make_event, make_user, login
):
    from app.core.permissions import ADMINISTRATOR

    city, website = town
    event = _flagged(db_session, make_event, city, website, "Listed")
    editor = make_user(email="editor@example.com", role_name=ADMINISTRATOR)
    check = _recorded_check(db_session, editor, [event.id])
    login("editor@example.com")
    token = _open_backfill_page(client)
    response = client.post(
        "/admin/events/all-day-backfill",
        data={"csrf_token": token, "mode": "apply", "check_id": str(check.id)},
    )
    assert response.status_code == 403
    db_session.refresh(event)
    assert event.all_day is False


# --- sources whose date-only entries mean "time not announced" -----------------

ATHLETICS_ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Athletics//Schedule//EN
BEGIN:VEVENT
UID:game-1@athletics.example.org
SUMMARY:Women's Tennis vs Rival College
DTSTART;VALUE=DATE:20270425
DTEND;VALUE=DATE:20270426
URL:https://athletics.example.org/game/1
END:VEVENT
BEGIN:VEVENT
UID:game-2@athletics.example.org
SUMMARY:Softball vs Rival College
DTSTART;TZID=America/New_York:20270424T150000
DTEND;TZID=America/New_York:20270424T170000
URL:https://athletics.example.org/game/2
END:VEVENT
END:VCALENDAR
"""
ATHLETICS_URL = "https://athletics.example.org/calendar.ics"


def _athletics_candidates(**config_values):
    from app.extraction.patterns.ics_calendar import IcsCalendarPattern

    config = SiteConfiguration(
        pattern_name="ics_calendar",
        api_endpoint=ATHLETICS_URL,
        timezone="America/New_York",
        **config_values,
    )
    response = make_response(ATHLETICS_ICS, final_url=ATHLETICS_URL, content_type="text/calendar")
    candidates = IcsCalendarPattern().extract(response, config)
    return {
        c.title: c
        for c in (
            normalize_candidate(n, config, fallback_timezone="America/New_York")
            for n in candidates
        )
    }


def test_ics_all_day_date_is_all_day_by_default():
    game = _athletics_candidates()["Women's Tennis vs Rival College"]
    assert ALL_DAY_MARKER in game.transformation_history


def test_time_not_announced_setting_keeps_date_only_games_time_tbd():
    from app.extraction.normalize import TIME_TBD_MARKER

    games = _athletics_candidates(date_only_means_time_tbd=True)
    tennis = games["Women's Tennis vs Rival College"]
    assert ALL_DAY_MARKER not in tennis.transformation_history
    assert TIME_TBD_MARKER in tennis.transformation_history
    assert tennis.start_time is None and tennis.end_time is None
    softball = games["Softball vs Rival College"]
    assert softball.start_time == time(15)
    assert TIME_TBD_MARKER not in softball.transformation_history


def test_time_not_announced_setting_clears_a_stored_all_day_flag(db_session, make_city, make_event):
    city = make_city(name="Princeton", slug="princeton-nj", timezone="America/New_York")
    event = make_event(city, start_date=date(2027, 4, 25), all_day=True)
    tennis = _athletics_candidates(date_only_means_time_tbd=True)[
        "Women's Tennis vs Rival College"
    ]
    update_event(db_session, event, tennis)
    assert event.all_day is False
    assert event.start_time is None


def test_time_not_announced_games_create_as_time_tbd(db_session, make_city, make_website):
    city = make_city(name="Princeton", slug="princeton-nj", timezone="America/New_York")
    website = make_website(city, name="Athletics")
    tennis = _athletics_candidates(date_only_means_time_tbd=True)[
        "Women's Tennis vs Rival College"
    ]
    event = create_event_from_candidate(
        db_session, tennis, source="Athletics", city_id=city.id, website_id=website.id
    )
    assert event.all_day is False and event.start_time is None


def test_backfill_leaves_time_not_announced_sources_as_time_tbd(
    db_session, make_city, make_website, make_event
):
    city = make_city(name="Princeton", slug="princeton-nj", timezone="America/New_York")
    athletics = make_website(
        city,
        name="Athletics",
        approved_pattern={
            "configuration": {
                "pattern_name": "ics_calendar",
                "api_endpoint": ATHLETICS_URL,
                "date_only_means_time_tbd": True,
            }
        },
    )
    campus = make_website(city, name="Campus", approved_pattern={"configuration": {}})
    game = make_event(city, website=athletics, title="Game", start_date=date(2027, 4, 25))
    holiday = make_event(city, website=campus, title="Holiday", start_date=date(2027, 4, 25))
    _provenance(db_session, game, athletics, [ALL_DAY_MARKER])
    _provenance(db_session, holiday, campus, [ALL_DAY_MARKER])

    report = backfill_all_day(db_session, apply=True)
    assert report.marked_ids == [holiday.id]
    assert sum(report.time_tbd_sources.values()) == 1
    db_session.refresh(game)
    assert game.all_day is False


def test_configure_form_round_trips_the_time_not_announced_setting():
    from app.schemas.extraction_config_forms import (
        build_site_configuration,
        configuration_to_form,
    )

    config = SiteConfiguration(
        pattern_name="ics_calendar", api_endpoint=ATHLETICS_URL, date_only_means_time_tbd=True
    )
    form = configuration_to_form(config)
    assert form.date_only_means_time_tbd is True
    rebuilt = build_site_configuration(form, config).configuration
    assert rebuilt.date_only_means_time_tbd is True
    form.date_only_means_time_tbd = False
    assert build_site_configuration(form, config).configuration.date_only_means_time_tbd is False


def test_explicit_all_day_wording_still_counts_with_the_setting():
    """The setting rereads a bare date as "time not announced"; a source that
    says "All day" in words still gets "All day"."""
    config = LIBRARY_CONFIG.model_copy(update={"date_only_means_time_tbd": True})
    response = make_response(
        json.dumps([_library_record()]), final_url=ENDPOINT, content_type="application/json"
    )
    [candidate] = [
        normalize_candidate(c, config, fallback_timezone=TZ)
        for c in SimpleviewEventsPattern().extract(response, config)
    ]
    assert ALL_DAY_MARKER in candidate.transformation_history


def test_genuine_all_day_athletics_meet_with_the_setting_reads_time_tbd_not_all_day():
    """Sidearm .ics feeds write a three-day swim invitational (listed "All Day"
    on the school's schedule page) and a game whose time is TBA the same way:
    DTSTART;VALUE=DATE. With nothing in the feed to tell them apart, the
    setting shows both as "Time TBD", never a game as "All day"."""
    from app.extraction.patterns.ics_calendar import IcsCalendarPattern

    ics = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Athletics//Schedule//EN
BEGIN:VEVENT
UID:vcal_1-admin.athletics.example.org
SUMMARY:Women's Swimming and Diving at Invitational
DTSTART;VALUE=DATE:20261203
DTEND;VALUE=DATE:20261206
URL:https://athletics.example.org/calendar.aspx?game_id=1
END:VEVENT
BEGIN:VEVENT
UID:vcal_2-admin.athletics.example.org
SUMMARY:Fencing at Ivy League
DTSTART;VALUE=DATE:20270206
DTEND;VALUE=DATE:20270207
URL:https://athletics.example.org/calendar.aspx?game_id=2
END:VEVENT
END:VCALENDAR
"""
    config = SiteConfiguration(
        pattern_name="ics_calendar", api_endpoint=ATHLETICS_URL,
        timezone="America/New_York", date_only_means_time_tbd=True,
    )
    response = make_response(ics, final_url=ATHLETICS_URL, content_type="text/calendar")
    candidates = [
        normalize_candidate(c, config, fallback_timezone="America/New_York")
        for c in IcsCalendarPattern().extract(response, config)
    ]
    assert len(candidates) == 2
    for candidate in candidates:
        assert ALL_DAY_MARKER not in candidate.transformation_history
        assert candidate.start_time is None
