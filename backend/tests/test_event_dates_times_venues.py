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
from datetime import UTC, date, datetime, time, timedelta

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


@pytest.mark.asyncio
async def test_recheck_never_starts_a_browser(db_session, iu_source, make_event, monkeypatch):
    city, website = iu_source
    website.approved_pattern = CONFIG.model_copy(
        update={"execution_strategy": "browser"}
    ).model_dump(mode="json")
    db_session.commit()
    row = _stored_like_31925(make_event, city, website)

    def no_browser(*args, **kwargs):
        raise AssertionError("recheck must not use a browser transport")

    from app.services import extraction_runs

    for name in (
        "BrowserStructuredResponseFetchStrategy", "BrowserRenderFetchStrategy",
        "_DisabledBrowserFetchStrategy", "_build_fetch_strategy",
    ):
        monkeypatch.setattr(extraction_runs, name, no_browser)
    seen: list[str] = []
    with patched_http_fetch(_id_handler({"2410954": RECITAL}, seen)):
        report = await recheck_website_events(
            db_session, website, apply=True, today=date(2026, 11, 20)
        )
    assert seen == [record_url(FEED, "2410954")]
    db_session.refresh(row)
    assert (row.start_date, row.start_time) == (date(2026, 11, 19), time(19))
    assert report.reviews == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("returned", "reason"),
    [
        # The endpoint answered with another event's record.
        ({"id": 2410955}, "the source returned a different event id"),
        # Same id, but a different page (or title) than the stored row.
        ({"url": "https://events.iu.edu/musiciub/event/2410954-another-recital"},
         "the source's title or page for this id differs"),
        ({"title": "Doctoral Recital"}, "the source's title or page for this id differs"),
        # Stored as one event, but the source says it repeats.
        ({"repeats": "weekly"}, "a repeating series stored as one row"),
    ],
)
async def test_recheck_requires_every_identity_to_match(
    db_session, iu_source, make_event, returned, reason
):
    city, website = iu_source
    row = _stored_like_31925(make_event, city, website)
    with patched_http_fetch(_id_handler({"2410954": _event(**returned)}, [])):
        report = await recheck_website_events(
            db_session, website, apply=True, today=date(2026, 11, 20)
        )
    assert report.changes == []
    assert [(r.event_id, r.reason) for r in report.reviews] == [(row.id, reason)]
    db_session.refresh(row)
    assert (row.start_date, row.start_time) == (date(2026, 11, 20), time(0))


@pytest.mark.asyncio
async def test_recheck_session_must_match_series_page_and_date(
    db_session, iu_source, make_event
):
    city, website = iu_source
    weekly = _event(id=2500001, repeats="weekly", title="Weekly Jam",
                    url="https://events.iu.edu/x/event/2500001-weekly-jam")
    # Right series and date, but stored under another page.
    wrong_page = make_event(
        city, website=website, title="Weekly Jam", external_source_id="2500001@2026-11-19",
        recurrence_parent_id="2500001", canonical_url="https://events.iu.edu/x/event/999-other",
        start_date=date(2026, 11, 19), start_time=time(0),
    )
    with patched_http_fetch(_id_handler({"2500001": weekly}, [])):
        report = await recheck_website_events(
            db_session, website, apply=True, today=date(2026, 11, 20)
        )
    assert report.changes == []
    assert [r.event_id for r in report.reviews] == [wrong_page.id]
    db_session.refresh(wrong_page)
    assert wrong_page.start_time == time(0)


# --- Scheduled imports keep events right without a recheck --------------------
#
# The scheduler runs app.services.scheduler_runner -> run_extraction, the same
# pipeline these tests drive: fetch the feed, extract, normalize, update_event.


def _listing_handler(*records: dict, seen: list[str] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(str(request.url))
        if "/live/json/events" in request.url.path:
            body = _feed(*records) if request.url.params.get("page", "1") == "1" else _feed()
            return httpx.Response(200, text=body, headers={"content-type": "application/json"})
        return httpx.Response(404, text="")

    return handler


NEW_EVENT = _event(
    id=2600001, title="Evening Jam Session",
    url="https://events.iu.edu/musiciub/event/2600001-evening-jam-session",
    date_iso="2026-12-03T20:30:00-05:00", date_ts=None,
    date2_iso=None, date2_ts=1796349600,  # 9:00 PM EST, end only as a timestamp
    location="TBD", location_title="Musical Arts Center",
)


@pytest.mark.asyncio
async def test_scheduled_import_corrects_stale_rows_and_new_events_arrive_right(
    db_session, iu_source, make_event
):
    from app.services.extraction_runs import run_extraction

    city, website = iu_source
    row = _stored_like_31925(make_event, city, website)
    update_fingerprint_and_duplicates(db_session, row)
    seen: list[str] = []
    with patched_http_fetch(_listing_handler(RECITAL, NEW_EVENT, seen=seen)):
        result = await run_extraction(db_session, website, triggered_by_user_id=None)

    assert result.status == "success"
    assert "/response_fields/" in seen[0]  # the feed is asked for locations
    db_session.refresh(row)
    assert (row.start_date, row.start_time, row.end_time) == (
        date(2026, 11, 19), time(19), time(20)
    )
    assert row.venue == "Recital Hall & LIVE@jacobs"
    new = db_session.query(Event).filter(Event.external_source_id == "2600001").one()
    assert (new.start_date, new.start_time) == (date(2026, 12, 3), time(20, 30))
    assert (new.end_date, new.end_time) == (date(2026, 12, 3), time(21))
    assert new.venue == "Musical Arts Center"  # "TBD" didn't block the real place
    assert new.timezone == INDY
    assert db_session.query(Event).count() == 2


@pytest.mark.asyncio
async def test_later_imports_never_revert_a_corrected_event(db_session, iu_source, make_event):
    from app.services.extraction_runs import run_extraction

    city, website = iu_source
    row = _stored_like_31925(make_event, city, website)
    update_fingerprint_and_duplicates(db_session, row)
    with patched_http_fetch(_listing_handler(RECITAL)):
        await run_extraction(db_session, website, triggered_by_user_id=None)
    db_session.refresh(row)
    corrected = (row.start_date, row.start_time, row.end_date, row.end_time, row.venue)
    changed_at = row.content_updated_at

    # 1. The same feed again: nothing moves, and no "details changed".
    with patched_http_fetch(_listing_handler(RECITAL)):
        again = await run_extraction(db_session, website, triggered_by_user_id=None)
    db_session.refresh(row)
    assert (row.start_date, row.start_time, row.end_date, row.end_time, row.venue) == corrected
    assert row.content_updated_at == changed_at
    assert again.events_inserted == 0

    # 2. A configuration saved with the old timestamp paths still reads date_iso.
    website.approved_pattern = CONFIG.model_copy(
        update={"json_paths": {"start_datetime": "date_ts", "end_datetime": "date2_ts"}}
    ).model_dump(mode="json")
    db_session.commit()
    with patched_http_fetch(_listing_handler(RECITAL)):
        await run_extraction(db_session, website, triggered_by_user_id=None)
    db_session.refresh(row)
    assert (row.start_date, row.start_time, row.end_date, row.end_time, row.venue) == corrected

    # 3. The feed briefly says "Venue TBD" / gives no location: the venue stays.
    for location in ("Venue TBD", None):
        with patched_http_fetch(_listing_handler(_event(location=location))):
            await run_extraction(db_session, website, triggered_by_user_id=None)
        db_session.refresh(row)
        assert row.venue == "Recital Hall & LIVE@jacobs"

    # 4. A bare midnight with no offset never replaces the real 7 PM.
    placeholder = _event(date_iso="2026-11-19T00:00:00", date2_iso=None,
                         date_ts=None, date2_ts=None)
    with patched_http_fetch(_listing_handler(placeholder)):
        await run_extraction(db_session, website, triggered_by_user_id=None)
    db_session.refresh(row)
    assert (row.start_date, row.start_time) == (date(2026, 11, 19), time(19))
    assert db_session.query(Event).count() == 1


def test_recheck_runs_only_when_an_admin_asks():
    """Imports never call the recheck; only the admin route does."""
    import inspect

    from app.scheduler import runtime
    from app.services import extraction_runs, scheduler_runner

    for module in (extraction_runs, scheduler_runner, runtime):
        assert "recheck_website_events" not in inspect.getsource(module)


# --- A recheck records what it confirmed, so the next batch moves on ---------


def _record(record_id: int, *, day: int, hour: int) -> dict:
    """A one-hour IU event on Nov `day` at `hour`:00 Eastern, with a venue."""
    start = f"2026-11-{day:02d}T{hour:02d}:00:00-05:00"
    end = f"2026-11-{day:02d}T{hour + 1:02d}:00:00-05:00"
    return _event(
        id=record_id, title=f"Event {record_id}",
        url=f"https://events.iu.edu/x/event/{record_id}-event",
        date_iso=start, date2_iso=end, date_ts=None, date2_ts=None,
        date_utc=None, date2_utc=None,
    )


def _stored(make_event, city, website, record: dict, **values) -> Event:
    defaults = {
        "start_date": date(2026, 11, 21), "start_time": time(0), "timezone": "EST",
        "scraped_at": datetime(2026, 11, 19, 15, 18, tzinfo=UTC),
    }
    return make_event(
        city, website=website, title=record["title"], canonical_url=record["url"],
        external_source_id=str(record["id"]), **{**defaults, **values},
    )


def _already_right(make_event, city, website, record: dict, **values) -> Event:
    """Stored exactly as the source has it, but still suspect (a real 12:00 AM
    start), so the recheck reads it back and finds nothing to change."""
    return _stored(
        make_event, city, website, record, start_date=date(2026, 11, 21),
        start_time=time(0), end_date=date(2026, 11, 21), end_time=time(1),
        timezone=INDY, venue="Recital Hall & LIVE@jacobs", **values,
    )


@pytest.mark.asyncio
async def test_recheck_records_corrected_and_already_right_events_as_checked(
    db_session, iu_source, make_event
):
    city, website = iu_source
    wrong = _record(2600001, day=21, hour=19)
    right = _record(2600002, day=21, hour=0)
    gone = _record(2600003, day=21, hour=19)
    edited_on = datetime(2026, 11, 1, 12, tzinfo=UTC)
    seen_on = datetime(2026, 11, 19, 15, 18, tzinfo=UTC)
    corrected = _stored(make_event, city, website, wrong, start_date=date(2026, 11, 22))
    unchanged = _already_right(make_event, city, website, right, content_updated_at=edited_on)
    failed = _stored(make_event, city, website, gone, content_updated_at=edited_on)
    records = {"2600001": wrong, "2600002": right}  # 2600003 is no longer returned
    today = date(2026, 11, 20)

    with patched_http_fetch(_id_handler(records, [])):
        preview = await recheck_website_events(db_session, website, today=today)
    assert (len(preview.changes), preview.unchanged, len(preview.reviews)) == (1, 1, 1)
    for row in (corrected, unchanged, failed):
        db_session.refresh(row)
        assert row.scraped_at.replace(tzinfo=UTC) == seen_on  # a preview marks nothing
    assert db_session.query(EventProvenance).count() == 0

    with patched_http_fetch(_id_handler(records, [])):
        report = await recheck_website_events(db_session, website, apply=True, today=today)
    for row in (corrected, unchanged, failed):
        db_session.refresh(row)

    # Corrected: details fixed, seen by a check, details-changed time set.
    assert (corrected.start_date, corrected.start_time) == (date(2026, 11, 21), time(19))
    assert corrected.scraped_at.replace(tzinfo=UTC) > seen_on
    assert corrected.content_updated_at is not None
    assert corrected.external_source_id == "2600001"
    # Already right: seen by a check, but its details-changed time is untouched.
    assert (unchanged.start_date, unchanged.start_time) == (date(2026, 11, 21), time(0))
    assert unchanged.scraped_at.replace(tzinfo=UTC) > seen_on
    assert unchanged.content_updated_at.replace(tzinfo=UTC) == edited_on
    # Not found at the source: not marked at all.
    assert (failed.start_date, failed.start_time) == (date(2026, 11, 21), time(0))
    assert failed.scraped_at.replace(tzinfo=UTC) == seen_on
    assert failed.content_updated_at.replace(tzinfo=UTC) == edited_on

    history = {
        p.event_id: p.transformation_history
        for p in db_session.query(EventProvenance).filter_by(extraction_run_id=report.run_id)
    }
    assert set(history) == {corrected.id, unchanged.id}
    assert "recheck:start_date" in history[corrected.id]
    assert "recheck:confirmed" in history[unchanged.id]
    assert db_session.query(Event).count() == 3

    # Only the event that could not be confirmed is selected again.
    with patched_http_fetch(_id_handler(records, [])):
        again = await recheck_website_events(db_session, website, today=today)
    assert again.selected == 1
    assert [r.event_id for r in again.reviews] == [failed.id]


@pytest.mark.asyncio
async def test_recheck_rows_that_need_review_are_never_marked(
    db_session, iu_source, make_event
):
    city, website = iu_source
    row = _stored_like_31925(make_event, city, website)
    seen_on = row.scraped_at.replace(tzinfo=UTC)
    # The id answers, but with another title: a person has to look.
    with patched_http_fetch(_id_handler({"2410954": _event(title="Other")}, [])):
        report = await recheck_website_events(
            db_session, website, apply=True, today=date(2026, 11, 20)
        )
    assert report.run_id is None and len(report.reviews) == 1
    db_session.refresh(row)
    assert row.scraped_at.replace(tzinfo=UTC) == seen_on
    assert row.content_updated_at is None
    assert db_session.query(EventProvenance).count() == 0


@pytest.mark.asyncio
async def test_successive_recheck_batches_move_on_to_unchecked_events(
    db_session, iu_source, make_event
):
    city, website = iu_source
    records = {
        str(record_id): _record(record_id, day=21, hour=0)
        for record_id in (2700001, 2700002, 2700003)
    }
    rows = [_already_right(make_event, city, website, r) for r in records.values()]
    # The third one is stored wrong and gets corrected in its batch.
    rows[2].start_date = date(2026, 11, 22)
    db_session.commit()
    today = date(2026, 11, 20)

    async def run(apply: bool) -> tuple[list[str], object]:
        seen: list[str] = []
        with patched_http_fetch(_id_handler(records, seen)):
            report = await recheck_website_events(
                db_session, website, apply=apply, limit=1, today=today
            )
        return [url.split("/id/", 1)[1].split("/", 1)[0] for url in seen], report

    # A preview never advances: the same first event every time.
    assert (await run(False))[0] == ["2700001"]
    assert (await run(False))[0] == ["2700001"]

    asked, report = await run(True)
    assert asked == ["2700001"] and report.unchanged == 1 and report.over_limit == 2
    asked, report = await run(True)
    assert asked == ["2700002"] and report.unchanged == 1 and report.over_limit == 1
    asked, report = await run(True)
    assert asked == ["2700003"] and len(report.changes) == 1 and report.over_limit == 0

    # Everything is confirmed: nothing left to read back.
    asked, report = await run(False)
    assert asked == [] and report.selected == 0
    db_session.refresh(rows[2])
    assert rows[2].start_date == date(2026, 11, 21)
    assert [r.external_source_id for r in rows] == list(records)


@pytest.mark.asyncio
async def test_a_confirmed_event_is_checked_again_after_its_details_change(
    db_session, iu_source, make_event
):
    city, website = iu_source
    record = _record(2800001, day=21, hour=0)
    row = _already_right(make_event, city, website, record)
    today = date(2026, 11, 20)
    with patched_http_fetch(_id_handler({"2800001": record}, [])):
        await recheck_website_events(db_session, website, apply=True, today=today)
        assert (await recheck_website_events(db_session, website, today=today)).selected == 0
    # A later change (an import, an administrator) makes it worth reading again.
    row.content_updated_at = datetime.now(UTC)
    db_session.commit()
    with patched_http_fetch(_id_handler({"2800001": record}, [])):
        assert (await recheck_website_events(db_session, website, today=today)).selected == 1


@pytest.mark.asyncio
async def test_a_confirmed_ordinary_event_is_read_again_after_a_later_edit(
    db_session, iu_source, make_event
):
    """Not at midnight, with a real venue, and seen by the latest import: only
    the later change to its details makes it worth reading back again."""
    from app.repositories.extraction_run import create_extraction_run

    city, website = iu_source
    record = _record(2900001, day=21, hour=19)
    row = _stored(
        make_event, city, website, record, start_time=time(19),
        end_date=date(2026, 11, 21), end_time=time(20), timezone=INDY,
        venue="Recital Hall & LIVE@jacobs",
    )
    # An import ran after the row was last seen, so the row is stale.
    create_extraction_run(
        db_session, website_id=website.id, configuration_version=None,
        pattern_name="livewhale_json", run_type="manual", status="success",
        source_url=FEED, final_url=FEED, events_found=0, events_valid=0,
        started_at=datetime(2026, 11, 20, 9, tzinfo=UTC),
        completed_at=datetime(2026, 11, 20, 9, 1, tzinfo=UTC),
    )
    today = date(2026, 11, 20)
    with patched_http_fetch(_id_handler({"2900001": record}, [])):
        assert (await recheck_website_events(db_session, website, today=today)).selected == 1
        confirmed = await recheck_website_events(db_session, website, apply=True, today=today)
        assert confirmed.unchanged == 1
        assert (await recheck_website_events(db_session, website, today=today)).selected == 0

    # A later edit (an import or an administrator) changes a detail and is
    # seen now: not midnight, has a venue, not stale, yet worth reading again.
    later = datetime.now(UTC) + timedelta(seconds=1)
    row.venue = "Auer Hall"
    row.content_updated_at = later
    row.scraped_at = later
    db_session.commit()
    seen: list[str] = []
    with patched_http_fetch(_id_handler({"2900001": record}, seen)):
        again = await recheck_website_events(db_session, website, today=today)
    assert again.selected == 1 and seen == [record_url(FEED, "2900001")]
    assert [c.event_id for c in again.changes] == [row.id]  # a preview proposes, never writes
    db_session.refresh(row)
    assert row.venue == "Auer Hall"
