"""Event dates, times and venues read correctly, and stored ones repaired.

Built around a real case: IU's "Master's Recital: Michaela West Cheek, viola"
(LiveWhale id 2410954) is Thursday Nov 19, 2026, 7 to 8 PM Eastern, in
"Recital Hall & LIVE@jacobs". Bulletin event 31925 showed it on Friday Nov 20
at 12:00 AM with no venue: an import before #96 read `date_ts` (00:00 UTC on
the 20th) as a local time and never asked the feed for locations, and the
event left the upcoming-events feed before any later import could fix it.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, date, datetime, time

import httpx
import pytest

from app.extraction.event_page_fallback import fill_from_event_pages, time_from_page, time_gap
from app.extraction.normalize import END_ABSENT_MARKER, normalize_candidate
from app.extraction.patterns.livewhale_json import LiveWhalePattern, record_url
from app.extraction.types import EventCandidate
from app.models.event import Event
from app.models.event_provenance import EventProvenance
from app.repositories.event import update_event
from app.schemas.extraction import FetchConfig, SiteConfiguration
from app.services.event_recheck import recheck_website_events
from app.services.fingerprints import update_fingerprint_and_duplicates
from tests.extraction_helpers import make_response, patched_http_fetch

INDY = "America/Indiana/Indianapolis"
FEED = "https://events.iu.edu/live/json/events"
CONFIG = SiteConfiguration(pattern_name="livewhale_json", api_endpoint=FEED)
PAGE = "https://events.iu.edu/musiciub/event/2410954-masters-recital-michaela-west-cheek-viola"

# The record as /live/json/events/id/2410954 returned it on 2026-11-20.
RECITAL = {
    "id": 2410954,
    "title": "Master’s Recital – Michaela West Cheek, viola",
    "url": PAGE,
    "date_utc": "2026-11-20 00:00:00",
    "date_iso": "2026-11-19T19:00:00-05:00",
    "date_ts": 1795132800,
    "timezone": "America/New_York",
    "date2_utc": "2026-11-20 01:00:00",
    "date2_iso": "2026-11-19T20:00:00-05:00",
    "date2_ts": 1795136400,
    "is_all_day": None,
    "repeats": None,
    "is_canceled": None,
    "location": "Recital Hall &amp; LIVE@jacobs",
    "location_title": None,
    "event_types": ["Performances, shows, and films"],
}


def _feed(*events: dict) -> str:
    return json.dumps({"meta": {"total_results": len(events)}, "data": list(events)})


def _normalized(event: dict, config: SiteConfiguration = CONFIG) -> EventCandidate:
    response = make_response(_feed(event), final_url=FEED, content_type="application/json")
    config = config.model_copy(update={"timezone": config.timezone or INDY})
    (candidate,) = LiveWhalePattern().extract(response, config)
    return normalize_candidate(candidate, config, fallback_timezone=INDY)


def _event(**changes) -> dict:
    record = copy.deepcopy(RECITAL)
    record.update(changes)
    return record


# --- Dates and times ---------------------------------------------------------


def test_iu_recital_keeps_its_own_evening_day_time_and_venue():
    event = _normalized(RECITAL)
    assert (event.start_date, event.start_time) == (date(2026, 11, 19), time(19))
    assert (event.end_date, event.end_time) == (date(2026, 11, 19), time(20))
    assert event.timezone == INDY
    assert event.venue == "Recital Hall & LIVE@jacobs"
    assert event.field_source_paths["start_datetime"].endswith(".date_iso")
    assert event.field_source_paths["end_datetime"].endswith(".date2_iso")
    assert not any(w.startswith("livewhale_time_mismatch") for w in event.warnings)


def test_timestamp_alone_is_the_same_instant_on_the_towns_clock():
    event = _normalized(_event(date_iso=None, date2_iso=None))
    assert (event.start_date, event.start_time) == (date(2026, 11, 19), time(19))
    assert (event.end_date, event.end_time) == (date(2026, 11, 19), time(20))
    assert event.field_source_paths["start_datetime"].endswith(".date_ts")


def test_timestamp_that_disagrees_with_the_iso_value_is_reported_not_used():
    event = _normalized(_event(date_ts=1795132800 + 3600))
    assert event.start_time == time(19)
    assert "livewhale_time_mismatch:start_datetime:date_iso!=date_ts" in event.warnings


def test_saved_json_path_no_longer_discards_the_iso_value():
    # A configuration saved before date_iso was read names the timestamp.
    config = CONFIG.model_copy(
        update={"json_paths": {"start_datetime": "date_ts", "end_datetime": "date2_ts"}}
    )
    event = _normalized(RECITAL, config)
    assert (event.start_date, event.start_time) == (date(2026, 11, 19), time(19))
    assert event.end_time == time(20)
    assert event.field_source_paths["start_datetime"].endswith(".date_iso")


def test_saved_json_path_is_still_used_when_the_feed_has_no_iso_value():
    config = CONFIG.model_copy(update={"json_paths": {"start_datetime": "date_utc"}})
    event = _normalized(_event(date_iso=None, date_ts=None), config)
    # date_utc is a naive UTC wall clock; it is read as UTC, not as local time.
    assert (event.start_date, event.start_time) == (date(2026, 11, 19), time(19))
    assert event.field_source_paths["start_datetime"].endswith(".date_utc")


@pytest.mark.parametrize(
    ("iso", "ts", "expected"),
    [
        # Just after UTC midnight in winter (EST, UTC-5): still the evening before.
        ("2026-12-03T19:30:00-05:00", 1796344200, (date(2026, 12, 3), time(19, 30))),
        # Summer (EDT, UTC-4): 11 PM local is 03:00 UTC the next day.
        ("2026-07-09T23:00:00-04:00", 1783652400, (date(2026, 7, 9), time(23))),
        # A real 12:30 AM start stays on its own (later) day.
        ("2026-11-21T00:30:00-05:00", 1795239000, (date(2026, 11, 21), time(0, 30))),
    ],
)
def test_utc_midnight_est_and_edt(iso, ts, expected):
    assert datetime.fromisoformat(iso).timestamp() == ts
    for record in (_event(date_iso=iso, date_ts=ts), _event(date_iso=None, date_ts=ts)):
        event = _normalized({**record, "date2_iso": None, "date2_ts": None})
        assert (event.start_date, event.start_time) == expected


def test_end_is_read_independently_of_the_start():
    # Start only as a timestamp, end only as an ISO value.
    event = _normalized(_event(date_iso=None, date2_ts=None))
    assert (event.start_date, event.start_time) == (date(2026, 11, 19), time(19))
    assert (event.end_date, event.end_time) == (date(2026, 11, 19), time(20))


def test_missing_end_is_marked_absent_not_invented():
    event = _normalized(_event(date2_iso=None, date2_ts=None))
    assert event.end_date is None and event.end_time is None
    assert END_ABSENT_MARKER in event.transformation_history


def test_all_day_event_is_dates_only_and_midnight_is_not_all_day():
    all_day = _normalized(
        _event(is_all_day=1, date_iso="2026-08-28T00:00:00-04:00",
               date2_iso="2026-12-03T00:00:00-05:00")
    )
    assert (all_day.start_date, all_day.end_date) == (date(2026, 8, 28), date(2026, 12, 3))
    assert all_day.start_time is None and all_day.end_time is None
    # A timed event that happens to start at midnight keeps its midnight.
    midnight = _normalized(_event(date_iso="2026-11-21T00:00:00-05:00", date2_iso=None))
    assert midnight.start_time == time(0)


def test_date_only_value_stays_a_date():
    config = CONFIG.model_copy(update={"json_paths": {"start_datetime": "day"}})
    event = _normalized(_event(date_iso=None, date_ts=None, day="2026-11-19"), config)
    assert event.start_date == date(2026, 11, 19)
    assert event.start_time is None


def test_wordpress_gmt_dates_are_read_as_utc():
    from app.extraction.patterns.wordpress_rest import WordPressRestPattern

    config = SiteConfiguration(pattern_name="wordpress_rest", api_endpoint="https://x.org/wp")
    body = json.dumps([{"id": 1, "title": {"rendered": "Talk"}, "link": "https://x.org/talk",
                        "date_gmt": "2026-11-20T00:00:00"}])
    (candidate,) = WordPressRestPattern().extract(
        make_response(body, final_url="https://x.org/wp", content_type="application/json"),
        config,
    )
    event = normalize_candidate(candidate, config, fallback_timezone=INDY)
    assert (event.start_date, event.start_time) == (date(2026, 11, 19), time(19))


def test_repeating_sessions_keep_their_own_keys():
    first = _event(repeats="weekly")
    second = _event(repeats="weekly", date_iso="2026-11-26T19:00:00-05:00",
                    date2_iso="2026-11-26T20:00:00-05:00")
    response = make_response(_feed(first, second), final_url=FEED,
                             content_type="application/json")
    config = CONFIG.model_copy(update={"timezone": INDY})
    pattern = LiveWhalePattern()
    candidates = pattern.finalize_candidates(pattern.extract(response, config), config)
    assert [c.raw["external_source_id"] for c in candidates] == [
        "2410954@2026-11-19", "2410954@2026-11-26",
    ]
    assert {c.recurrence_parent_id for c in candidates} == {"2410954"}


# --- Venues ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "venue"),
    [
        ({"location": None, "location_title": "Auer Hall"}, "Auer Hall"),
        ({"location": "", "location_title": "Auer Hall"}, "Auer Hall"),
        ({"location": "TBD", "location_title": "Auer Hall"}, "Auer Hall"),
        ({"location": "Location TBA", "place": {"title": "Auer Hall"}}, "Auer Hall"),
        ({"location": {"title": "Musical Arts Center", "id": 7}}, "Musical Arts Center"),
        ({"location": "Venue TBD"}, None),
        ({"location": None}, None),
    ],
)
def test_venue_fallbacks_skip_placeholders(changes, venue):
    assert _normalized(_event(**changes)).venue == venue


# --- Detail pages ------------------------------------------------------------


# The IU page's own markup, trimmed: JSON-LD names only the online stream, and
# its startDate is the right instant written in UTC.
DETAIL_HTML = """<html><head>
<meta name="generator" content="LiveWhale Calendar" />
<meta property="og:start_time" content="2026-11-19T19:00:00-05:00" />
<script type="application/ld+json">{"@context": "http://schema.org/", "@type": "Event",
 "name": "Master’s Recital", "startDate": "2026-11-20T00:00:00+00:00",
 "endDate": "2026-11-20T01:00:00+00:00", "url": "PAGE_URL",
 "location": [{"@type": "VirtualLocation", "url": "https://liveatjacobs.music.indiana.edu/"}]}
</script></head><body>
<div id="lw_cal_event_detail_meta">
 <div class="lw_cal_event_detail_meta_item"><strong><time><span class="lw_start_time">7:00 pm</span>
  - <span class="lw_end_time">8:00 pm</span></time></strong></div>
 <div class="lw_cal_event_detail_meta_item"><strong>Recital Hall &amp; LIVE@jacobs</strong></div>
</div></body></html>""".replace("PAGE_URL", PAGE)


class _PageFetch:
    def __init__(self, pages: dict[str, str]):
        self.pages = pages
        self.fetched: list[str] = []

    async def fetch(self, request, config):
        self.fetched.append(request.url)
        body = self.pages.get(request.url)
        return make_response(body or "", final_url=request.url, status_code=200 if body else 404)


def _listing(raw: dict) -> EventCandidate:
    base = {"title": "Master’s Recital", "canonical_url": PAGE,
            "description": "A recital on viola with piano, streamed live."}
    return EventCandidate(
        raw={**base, **raw}, title=None, canonical_url=None, description=None,
        start_date=None, start_time=None, end_date=None, end_time=None, timezone=None,
        venue=None, address=None, image_url=None, latitude=None, longitude=None,
        source_category=None, external_source_id=None, field_source_paths={},
        transformation_history=(), source_page="https://events.iu.edu/", extraction_pattern="x",
        warnings=(), raw_record_hash="h",
    )


def test_page_time_prefers_jsonld_and_keeps_its_offset():
    found = time_from_page(DETAIL_HTML, PAGE)
    assert found.start == "2026-11-20T00:00:00+00:00"
    assert found.source == "detail:jsonld.startDate"


@pytest.mark.asyncio
async def test_placeholder_midnight_and_missing_venue_filled_from_the_page():
    fetch = _PageFetch({PAGE: DETAIL_HTML})
    (filled,), warnings = await fill_from_event_pages(
        [_listing({"start_datetime": "2026-11-19T00:00:00"})], fetch, FetchConfig(),
        timezone=INDY,
    )
    assert fetch.fetched == [PAGE]
    assert filled.raw["venue"] == "Recital Hall & LIVE@jacobs"
    assert filled.field_source_paths["venue"] == "detail:livewhale.event_detail_meta"
    assert filled.field_source_paths["start_datetime"] == "detail:jsonld.startDate"
    assert filled.field_source_paths["end_datetime"] == "detail:jsonld.endDate"
    event = normalize_candidate(filled, CONFIG.model_copy(update={"timezone": INDY}))
    assert (event.start_date, event.start_time, event.end_time) == (
        date(2026, 11, 19), time(19), time(20)
    )
    assert "time_lookup:1_filled_of_1_pages" in warnings


@pytest.mark.asyncio
async def test_page_about_another_day_does_not_move_the_event():
    fetch = _PageFetch({PAGE: DETAIL_HTML})
    (kept,), _ = await fill_from_event_pages(
        [_listing({"start_datetime": "2026-11-26T00:00:00"})], fetch, FetchConfig(),
        timezone=INDY,
    )
    assert kept.raw["start_datetime"] == "2026-11-26T00:00:00"


@pytest.mark.asyncio
async def test_offset_times_and_all_day_events_are_not_looked_up():
    fetch = _PageFetch({PAGE: DETAIL_HTML})
    listings = [
        _listing({"start_datetime": "2026-11-20T00:00:00+00:00", "venue": "Auer Hall"}),
        _listing({"start_datetime": "2026-11-19", "all_day": True, "venue": "Auer Hall"}),
        # Date-only with nothing else missing: not worth a fetch of its own.
        _listing({"start_datetime": "2026-11-19", "venue": "Auer Hall"}),
    ]
    assert time_gap(listings[0].raw) is None and time_gap(listings[1].raw) is None
    await fill_from_event_pages(listings, fetch, FetchConfig(), timezone=INDY)
    assert fetch.fetched == []


@pytest.mark.asyncio
async def test_page_without_a_venue_leaves_a_genuinely_missing_venue_empty():
    page = DETAIL_HTML.replace("Recital Hall &amp; LIVE@jacobs", "")
    page = page.replace('<div class="lw_cal_event_detail_meta_item"><strong></strong></div>', "")
    fetch = _PageFetch({PAGE: page})
    (kept,), _ = await fill_from_event_pages(
        [_listing({"start_datetime": "2026-11-19T19:00:00-05:00"})], fetch, FetchConfig(),
        timezone=INDY,
    )
    assert "venue" not in kept.raw


# --- Re-imports and stored records -------------------------------------------


@pytest.fixture
def iu_source(db_session, make_city, make_website):
    city = make_city(name="Bloomington", slug="bloomington-in", timezone=INDY)
    website = make_website(city, name="IU", base_url="https://events.iu.edu",
                           is_active=True, approved_pattern=CONFIG.model_dump(mode="json"))
    website.onboarding_status = "active"
    db_session.commit()
    return city, website


def _stored_like_31925(make_event, city, website, **values) -> Event:
    """As the pre-#96 import stored the recital."""
    row = make_event(
        city, website=website, title=RECITAL["title"], external_source_id="2410954",
        canonical_url=PAGE, start_date=date(2026, 11, 20), start_time=time(0),
        end_date=date(2026, 11, 20), end_time=time(1), timezone="EST",
        scraped_at=datetime(2026, 11, 19, 15, 18, tzinfo=UTC), **values,
    )
    return row


def test_reimport_clears_a_stale_end_the_source_says_is_absent(
    db_session, iu_source, make_event
):
    city, website = iu_source
    row = _stored_like_31925(make_event, city, website)
    update_event(db_session, row, _normalized(_event(date2_iso=None, date2_ts=None)))
    assert (row.start_date, row.start_time) == (date(2026, 11, 19), time(19))
    assert row.end_date is None and row.end_time is None


def test_reimport_never_replaces_a_real_time_with_a_bare_midnight(
    db_session, iu_source, make_event
):
    city, website = iu_source
    row = _stored_like_31925(make_event, city, website)
    row.start_date, row.start_time = date(2026, 11, 19), time(19)
    db_session.commit()
    placeholder = _normalized(_event(date_iso="2026-11-19T00:00:00", date2_iso=None,
                                     date_ts=None, date2_ts=None))
    assert placeholder.start_time == time(0)
    update_event(db_session, row, placeholder)
    assert row.start_time == time(19)


def _id_handler(records: dict[str, dict | None], seen: list[str]):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        record_id = request.url.path.split("/id/", 1)[1].split("/", 1)[0]
        record = records.get(record_id)
        body = _feed(record) if record else _feed()
        return httpx.Response(200, text=body, headers={"content-type": "application/json"})

    return handler


@pytest.mark.asyncio
async def test_recheck_previews_then_corrects_31925_in_place(
    db_session, iu_source, make_event
):
    city, website = iu_source
    row = _stored_like_31925(make_event, city, website)
    row.corrected_address = "1201 E 3rd St, Bloomington, IN"  # an admin correction
    update_fingerprint_and_duplicates(db_session, row)
    seen: list[str] = []
    today = date(2026, 11, 20)

    with patched_http_fetch(_id_handler({"2410954": RECITAL}, seen)):
        preview = await recheck_website_events(db_session, website, today=today)
    assert seen == [record_url(FEED, "2410954")]
    assert [c.event_id for c in preview.changes] == [row.id]
    assert preview.changes[0].changes["start_date"] == (date(2026, 11, 20), date(2026, 11, 19))
    db_session.refresh(row)
    assert row.start_date == date(2026, 11, 20)  # a preview changes nothing

    with patched_http_fetch(_id_handler({"2410954": RECITAL}, [])):
        report = await recheck_website_events(db_session, website, apply=True, today=today)
    assert report.run_id is not None
    db_session.refresh(row)
    assert (row.start_date, row.start_time) == (date(2026, 11, 19), time(19))
    assert (row.end_date, row.end_time) == (date(2026, 11, 19), time(20))
    assert row.timezone == INDY
    assert row.venue == "Recital Hall & LIVE@jacobs"
    assert row.corrected_address == "1201 E 3rd St, Bloomington, IN"
    assert row.external_source_id == "2410954"
    assert row.content_updated_at is not None  # visitors see "details changed"
    assert db_session.query(Event).count() == 1
    provenance = db_session.query(EventProvenance).filter_by(event_id=row.id).one()
    assert provenance.extraction_run_id == report.run_id
    assert provenance.field_source_paths["start_datetime"].endswith(".date_iso")
    assert "recheck:start_date" in provenance.transformation_history

    # Once corrected the event is past (and no longer suspect): nothing to do.
    with patched_http_fetch(_id_handler({"2410954": RECITAL}, [])):
        again = await recheck_website_events(db_session, website, today=today)
    assert again.changes == []


@pytest.mark.asyncio
async def test_recheck_keeps_session_ids_and_sends_unmatched_rows_to_review(
    db_session, iu_source, make_event
):
    city, website = iu_source
    weekly = _event(id=2500001, repeats="weekly", title="Weekly Jam",
                    url="https://events.iu.edu/x/event/2500001-weekly-jam")
    session = make_event(
        city, website=website, title="Weekly Jam", external_source_id="2500001@2026-11-19",
        recurrence_parent_id="2500001", canonical_url=weekly["url"],
        start_date=date(2026, 11, 19), start_time=time(0),
    )
    other_session = make_event(
        city, website=website, title="Weekly Jam", external_source_id="2500001@2026-11-26",
        recurrence_parent_id="2500001", canonical_url=weekly["url"],
        start_date=date(2026, 11, 26), start_time=time(0),
    )
    gone = make_event(
        city, website=website, title="Gone", external_source_id="2599999",
        canonical_url="https://events.iu.edu/x/event/2599999-gone",
        start_date=date(2026, 11, 21), start_time=time(0),
    )
    seen: list[str] = []
    with patched_http_fetch(_id_handler({"2500001": weekly}, seen)):
        report = await recheck_website_events(
            db_session, website, apply=True, today=date(2026, 11, 20)
        )
    # One request per source id, not per session.
    assert len(seen) == 2
    for row in (session, other_session, gone):
        db_session.refresh(row)
    assert (session.start_time, session.external_source_id) == (time(19), "2500001@2026-11-19")
    assert session.recurrence_parent_id == "2500001"
    assert other_session.start_time == time(0)
    assert gone.start_time == time(0) and gone.is_active
    reasons = {r.event_id: r.reason for r in report.reviews}
    assert reasons == {
        other_session.id: "the source did not return this session",
        gone.id: "the source no longer returns this event",
    }
    assert db_session.query(Event).count() == 3


def test_recheck_admin_preview_then_apply(
    client, make_super_admin, make_event, login, db_session, iu_source
):
    city, website = iu_source
    row = make_event(
        city, website=website, title=RECITAL["title"], external_source_id="2410954",
        canonical_url=PAGE, start_date=date.today(), start_time=time(0), timezone="EST",
    )
    record = _event(
        date_iso=f"{date.today().isoformat()}T19:00:00-05:00", date_ts=None,
        date2_iso=None, date2_ts=None,
    )
    make_super_admin(email="recheck@example.com", password="root-pass-1234")
    login("recheck@example.com", "root-pass-1234")
    assert "Preview recheck" in client.get(f"/admin/websites/{website.id}").text

    with patched_http_fetch(_id_handler({"2410954": record}, [])):
        preview = client.post(
            f"/admin/websites/{website.id}/recheck-events",
            data={"csrf_token": client.cookies.get("csrf_token"), "apply": "0"},
        )
        assert preview.status_code == 200
        assert "Apply these corrections" in preview.text
        db_session.refresh(row)
        assert row.start_time == time(0)
        applied = client.post(
            f"/admin/websites/{website.id}/recheck-events",
            data={"csrf_token": client.cookies.get("csrf_token"), "apply": "1"},
        )
    assert applied.status_code == 200 and "Corrected" in applied.text
    db_session.refresh(row)
    assert row.start_time == time(19)
