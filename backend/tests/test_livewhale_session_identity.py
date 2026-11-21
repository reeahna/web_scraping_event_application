"""Which IU (LiveWhale) records are sessions of a series, and how a stored
"<id>@<date>" row and a later plain-id record are matched.

The same record can appear twice in one run when feed pages overlap. That is
one event, not a series: it keeps its plain id. A row stored earlier under
"<id>@<date>" (from such a run) is reused when the event comes back under its
plain id, instead of a second row being created next to it."""

from __future__ import annotations

import json
from datetime import date, time

import httpx
import pytest

from app.extraction.patterns.livewhale_json import LiveWhalePattern
from app.models.event import Event
from app.schemas.extraction import SiteConfiguration
from app.services.extraction_runs import run_extraction
from app.services.fingerprints import update_fingerprint_and_duplicates
from tests.extraction_helpers import make_response, patched_http_fetch

INDY = "America/Indiana/Indianapolis"
FEED = "https://events.iu.edu/live/json/events"
CONFIG = SiteConfiguration(pattern_name="livewhale_json", api_endpoint=FEED)


def _record(event_id: int, day: str, hour: int = 19, title: str = "Wrestling vs Oklahoma",
            **extra) -> dict:
    return {
        "id": event_id, "title": title,
        "url": f"https://events.iu.edu/hoosiers/event/{event_id}-event",
        "date_iso": f"{day}T{hour:02d}:00:00-05:00",
        "date2_iso": f"{day}T{hour + 1:02d}:00:00-05:00",
        "timezone": INDY, "location_title": "Wilkinson Hall", **extra,
    }


def _page(records: list[dict], page: int = 1):
    url = FEED if page == 1 else f"{FEED}?page={page}"
    return make_response(json.dumps({"data": records}), final_url=url,
                         content_type="application/json")


def _keys(pages: list[list[dict]]) -> list[str]:
    pattern = LiveWhalePattern()
    candidates = []
    for number, records in enumerate(pages, start=1):
        candidates += pattern.extract(_page(records, number), CONFIG)
    return [c.raw["external_source_id"] for c in pattern.finalize_candidates(candidates, CONFIG)]


# --- keying -----------------------------------------------------------------


def test_a_single_event_keeps_its_plain_id():
    assert _keys([[_record(2434332, "2026-11-20")]]) == ["2434332"]


def test_the_same_record_on_two_overlapping_pages_is_one_event():
    record = _record(2434332, "2026-11-20")
    assert _keys([[record], [dict(record)]]) == ["2434332", "2434332"]


def test_the_same_record_twice_on_one_page_is_one_event():
    record = _record(2434332, "2026-11-20")
    pattern = LiveWhalePattern()
    keyed = pattern.extract(_page([record, dict(record)]), CONFIG)
    assert [c.raw["external_source_id"] for c in keyed] == ["2434332", "2434332"]
    assert {c.recurrence_parent_id for c in keyed} == {None}


def test_sessions_on_different_dates_are_a_series():
    pages = [[_record(3301, "2026-12-01")], [_record(3301, "2026-12-08"),
                                             _record(3301, "2026-12-01")]]
    assert _keys(pages) == ["3301@2026-12-01", "3301@2026-12-08", "3301@2026-12-01"]


def test_two_sessions_on_one_date_are_still_keyed_as_a_series():
    # A 10 AM and a 2 PM session share an id and a date: still a series.
    keys = _keys([[_record(3302, "2026-12-01", hour=10), _record(3302, "2026-12-01", hour=14)]])
    assert keys == ["3302@2026-12-01", "3302@2026-12-01"]


def test_a_record_the_feed_flags_as_repeating_is_a_series_even_alone():
    assert _keys([[_record(3303, "2026-12-01", repeats="weekly")]]) == ["3303@2026-12-01"]


# --- matching stored rows -----------------------------------------------------


@pytest.fixture
def iu_source(db_session, make_city, make_website):
    city = make_city(name="Bloomington", slug="bloomington-in", timezone=INDY)
    website = make_website(city, name="IU", base_url="https://events.iu.edu",
                           is_active=True, approved_pattern=CONFIG.model_dump(mode="json"))
    website.onboarding_status = "active"
    db_session.commit()
    return city, website


def _handler(records: list[dict]):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=json.dumps({"data": records}),
                              headers={"content-type": "application/json"})

    return handler


def _stored_session(db_session, make_event, city, website, event_id: int, day: date, **fields):
    row = make_event(
        city, website=website, title=fields.pop("title", "Wrestling vs Oklahoma"),
        external_source_id=f"{event_id}@{day.isoformat()}",
        canonical_url=f"https://events.iu.edu/hoosiers/event/{event_id}-event",
        start_date=day, start_time=time(19), timezone=INDY, **fields,
    )
    row.recurrence_parent_id = str(event_id)
    update_fingerprint_and_duplicates(db_session, row)
    db_session.commit()
    return row


# The nine plain-id rows production run #2186 created next to existing
# "<id>@<date>" rows (IU id, local date).
RUN_2186_CASES = [
    (2485608, "2026-11-20"), (2483616, "2026-11-24"), (2393966, "2026-11-24"),
    (2456287, "2026-11-24"), (2484557, "2026-11-27"), (2393965, "2026-11-27"),
    (2263467, "2026-11-27"), (2488420, "2026-12-01"), (2441880, "2026-12-01"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("event_id,day", RUN_2186_CASES)
async def test_plain_id_reuses_the_stored_session_row(
    db_session, iu_source, make_event, event_id, day
):
    city, website = iu_source
    stored = _stored_session(db_session, make_event, city, website, event_id,
                             date.fromisoformat(day))
    with patched_http_fetch(_handler([_record(event_id, day)])):
        result = await run_extraction(db_session, website, triggered_by_user_id=None)

    assert result.events_inserted == 0
    rows = db_session.query(Event).filter(Event.website_id == website.id).all()
    assert [r.id for r in rows] == [stored.id]
    assert rows[0].external_source_id == str(event_id)
    assert rows[0].recurrence_parent_id is None


@pytest.mark.asyncio
async def test_reused_row_keeps_its_corrections_visibility_and_source(
    db_session, iu_source, make_event
):
    city, website = iu_source
    stored = _stored_session(db_session, make_event, city, website, 2434332, date(2026, 11, 20))
    stored.corrected_venue = "Wilkinson Hall (main gym)"
    stored.review_status = "reviewed"
    db_session.commit()
    created_at = stored.created_at

    with patched_http_fetch(_handler([_record(2434332, "2026-11-20")])):
        await run_extraction(db_session, website, triggered_by_user_id=None)
    db_session.refresh(stored)

    assert stored.corrected_venue == "Wilkinson Hall (main gym)"
    assert stored.review_status == "reviewed"
    assert stored.is_active is True
    assert stored.website_id == website.id
    assert stored.created_at == created_at

    # A second run matches the same row by its plain id.
    with patched_http_fetch(_handler([_record(2434332, "2026-11-20")])):
        again = await run_extraction(db_session, website, triggered_by_user_id=None)
    assert again.events_inserted == 0
    assert db_session.query(Event).filter(Event.website_id == website.id).count() == 1


@pytest.mark.asyncio
async def test_existing_plain_and_session_rows_are_left_as_they_are(
    db_session, iu_source, make_event
):
    # Production already has both (for example #31973 and #35373): the plain
    # row is the match, and the session row is not merged, hidden or changed.
    city, website = iu_source
    session_row = _stored_session(db_session, make_event, city, website, 2434332,
                                  date(2026, 11, 20))
    plain_row = make_event(
        city, website=website, title="Wrestling vs Oklahoma", external_source_id="2434332",
        canonical_url="https://events.iu.edu/hoosiers/event/2434332-event",
        start_date=date(2026, 11, 20), start_time=time(19), timezone=INDY,
    )
    update_fingerprint_and_duplicates(db_session, plain_row)
    db_session.commit()

    with patched_http_fetch(_handler([_record(2434332, "2026-11-20")])):
        result = await run_extraction(db_session, website, triggered_by_user_id=None)
    db_session.refresh(session_row)

    assert result.events_inserted == 0
    assert session_row.external_source_id == "2434332@2026-11-20"
    assert session_row.is_active is True
    assert session_row.duplicate_status != "confirmed_duplicate"
    assert db_session.query(Event).filter(Event.website_id == website.id).count() == 2


@pytest.mark.asyncio
async def test_a_session_row_on_another_date_is_not_reused(db_session, iu_source, make_event):
    city, website = iu_source
    other_day = _stored_session(db_session, make_event, city, website, 2434332,
                                date(2026, 11, 19))
    with patched_http_fetch(_handler([_record(2434332, "2026-11-20")])):
        result = await run_extraction(db_session, website, triggered_by_user_id=None)
    db_session.refresh(other_day)

    assert result.events_inserted == 1
    assert other_day.external_source_id == "2434332@2026-11-19"


@pytest.mark.asyncio
async def test_a_session_row_from_another_source_is_not_reused(
    db_session, iu_source, make_event, make_website
):
    city, website = iu_source
    other = make_website(city, name="Other", base_url="https://other.example")
    elsewhere = _stored_session(db_session, make_event, city, other, 2434332, date(2026, 11, 20))
    with patched_http_fetch(_handler([_record(2434332, "2026-11-20")])):
        result = await run_extraction(db_session, website, triggered_by_user_id=None)
    db_session.refresh(elsewhere)

    assert result.events_inserted == 1
    assert elsewhere.website_id == other.id
    assert elsewhere.external_source_id == "2434332@2026-11-20"


@pytest.mark.asyncio
async def test_overlapping_pages_store_one_row(db_session, iu_source):
    _, website = iu_source
    paged = CONFIG.model_dump(mode="json")
    paged["pagination"] = {"strategy": "query_param", "page_param": "page", "max_pages": 3}
    website.approved_pattern = paged
    db_session.commit()
    record = _record(2434332, "2026-11-20")
    pages = {"1": [record, _record(2434333, "2026-11-21")], "2": [dict(record)]}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text=json.dumps({"data": pages.get(request.url.params.get("page", "1"), [])}),
            headers={"content-type": "application/json"},
        )

    with patched_http_fetch(handler):
        await run_extraction(db_session, website, triggered_by_user_id=None)
    rows = db_session.query(Event).filter(Event.website_id == website.id).order_by(Event.id).all()
    assert [r.external_source_id for r in rows] == ["2434332", "2434333"]


@pytest.mark.asyncio
async def test_a_single_event_becoming_a_series_still_takes_over_its_row(
    db_session, iu_source, make_event
):
    # The other direction is unchanged: a plain-id row becomes the first session.
    city, website = iu_source
    plain = make_event(
        city, website=website, title="Weekly Knitting Circle", external_source_id="3301",
        canonical_url="https://events.iu.edu/hoosiers/event/3301-event",
        start_date=date(2026, 12, 1), start_time=time(19), timezone=INDY,
    )
    update_fingerprint_and_duplicates(db_session, plain)
    db_session.commit()
    records = [_record(3301, "2026-12-01", title="Weekly Knitting Circle"),
               _record(3301, "2026-12-08", title="Weekly Knitting Circle")]
    with patched_http_fetch(_handler(records)):
        await run_extraction(db_session, website, triggered_by_user_id=None)
    db_session.refresh(plain)
    assert plain.external_source_id == "3301@2026-12-01"
    assert db_session.query(Event).filter(Event.website_id == website.id).count() == 2


@pytest.mark.asyncio
async def test_a_different_session_on_the_same_date_is_not_reused(
    db_session, iu_source, make_event
):
    # A 10 AM session was stored; the feed now lists only a 2 PM record under
    # the plain id. That may be another session, so the stored row is left.
    city, website = iu_source
    morning = _stored_session(db_session, make_event, city, website, 3302, date(2026, 12, 1))
    morning.start_time = time(10)
    update_fingerprint_and_duplicates(db_session, morning)
    db_session.commit()
    with patched_http_fetch(_handler([_record(3302, "2026-12-01", hour=14)])):
        result = await run_extraction(db_session, website, triggered_by_user_id=None)
    db_session.refresh(morning)

    assert result.events_inserted == 1
    assert morning.external_source_id == "3302@2026-12-01"
    assert morning.start_time == time(10)


@pytest.mark.asyncio
async def test_two_stored_sessions_on_one_date_are_not_reused(
    db_session, iu_source, make_event
):
    # Two rows share the key "3302@2026-12-01": which one is meant can't be
    # told, so neither is taken over.
    city, website = iu_source
    first = _stored_session(db_session, make_event, city, website, 3302, date(2026, 12, 1))
    second = _stored_session(db_session, make_event, city, website, 3302, date(2026, 12, 1))
    with patched_http_fetch(_handler([_record(3302, "2026-12-01")])):
        result = await run_extraction(db_session, website, triggered_by_user_id=None)
    for row in (first, second):
        db_session.refresh(row)
        assert row.external_source_id == "3302@2026-12-01"
    assert result.events_inserted == 1


@pytest.mark.asyncio
async def test_a_hidden_session_row_stays_hidden_when_reused(
    db_session, iu_source, make_event
):
    city, website = iu_source
    stored = _stored_session(db_session, make_event, city, website, 2441880, date(2026, 12, 1))
    stored.is_active = False
    db_session.commit()
    with patched_http_fetch(_handler([_record(2441880, "2026-12-01")])):
        result = await run_extraction(db_session, website, triggered_by_user_id=None)
    db_session.refresh(stored)

    assert result.events_inserted == 0
    assert stored.external_source_id == "2441880"
    assert stored.is_active is False
