"""ICS feeds identify events by UID and may leave URL out. Such an event is
valid, so the import must save it (with the source's address as its link)
instead of failing the whole run on the events table's required link."""

from datetime import date

import httpx

from app.extraction.types import EventCandidate
from app.extraction.validate import validate_candidate
from app.models.event import Event
from app.schemas.extraction import SiteConfiguration
from app.services.extraction_runs import preview_extraction, run_extraction
from app.services.website_configuration import approve_configuration
from tests.extraction_helpers import patched_http_fetch

FEED = "\r\n".join(
    [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Test//EN",
        "BEGIN:VEVENT",
        "UID:linked-1@example.com",
        "SUMMARY:Linked lecture",
        "DTSTART:20270310T190000Z",
        "URL:https://example.com/events/linked-lecture",
        "END:VEVENT",
        "BEGIN:VEVENT",
        "UID:unlinked-1@example.com",
        "SUMMARY:Unlinked concert",
        "DTSTART:20270311T200000Z",
        "END:VEVENT",
        "END:VCALENDAR",
        "",
    ]
)


def _handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, text=FEED, headers={"content-type": "text/calendar"})


async def _approved_ics_site(db_session, make_city, make_website, make_user):
    city = make_city()
    website = make_website(city, name="Feed Source", base_url="https://example.com/calendar")
    admin = make_user(email="ics-approver@example.com")
    website.configuration = SiteConfiguration(
        pattern_name="ics_calendar",
        api_endpoint="https://example.com/calendar.ics",
        fetch={"allowed_content_types": ["text/calendar", "text/plain"]},
        required_fields=["title", "start_date"],
    ).model_dump(mode="json")
    db_session.commit()
    with patched_http_fetch(_handler):
        await preview_extraction(db_session, website)
    approve_configuration(db_session, website, approved_by_user_id=admin.id)
    return website, admin


async def test_event_without_a_link_is_saved_with_the_source_address(
    db_session, make_city, make_website, make_user
):
    website, admin = await _approved_ics_site(db_session, make_city, make_website, make_user)

    with patched_http_fetch(_handler):
        result = await run_extraction(db_session, website, triggered_by_user_id=admin.id)

    assert result.status == "success"
    assert result.events_inserted == 2
    links = {
        e.title: e.canonical_url
        for e in db_session.query(Event).filter(Event.website_id == website.id)
    }
    assert links == {
        "Linked lecture": "https://example.com/events/linked-lecture",
        "Unlinked concert": "https://example.com/calendar",
    }


async def test_rerun_updates_events_without_a_link_instead_of_failing(
    db_session, make_city, make_website, make_user
):
    website, admin = await _approved_ics_site(db_session, make_city, make_website, make_user)

    with patched_http_fetch(_handler):
        await run_extraction(db_session, website, triggered_by_user_id=admin.id)
        second = await run_extraction(db_session, website, triggered_by_user_id=admin.id)

    assert second.status == "success"
    assert second.events_inserted == 0
    assert second.events_updated == 2
    unlinked = db_session.query(Event).filter(Event.title == "Unlinked concert").one()
    assert unlinked.canonical_url == "https://example.com/calendar"


def _candidate(external_source_id: str | None) -> EventCandidate:
    return EventCandidate(
        raw={}, title="Talk", canonical_url=None, description=None,
        start_date=date(2027, 3, 10), start_time=None, end_date=None, end_time=None,
        timezone=None, venue=None, address=None, image_url=None,
        latitude=None, longitude=None, source_category=None,
        external_source_id=external_source_id, field_source_paths={},
        transformation_history=(), source_page="https://example.com/calendar.ics",
        extraction_pattern="ics_calendar", warnings=(), raw_record_hash="h1",
    )


def test_event_with_neither_a_link_nor_a_source_id_is_rejected():
    config = SiteConfiguration(
        pattern_name="ics_calendar",
        api_endpoint="https://example.com/calendar.ics",
        required_fields=["title", "start_date"],
    )
    assert not validate_candidate(_candidate(None), config).is_valid
    assert validate_candidate(_candidate("uid-1"), config).is_valid
