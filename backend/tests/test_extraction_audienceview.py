"""AudienceView box-office pattern: extractor, detector, proposer, registry.

The fixture is a trimmed copy of the AudienceView "All Upcoming Events" page
that a university performing-arts center's /upcoming-events URL redirects to.
Its performances live in a `searchHeaders` / `searchResults` table inside a
script and are only drawn as cards by the page's own JavaScript, so a plain
HTTP fetch must read the table, never the (empty) card markup.
"""

from __future__ import annotations

import json

from app.extraction.dedup import dedupe_within_run
from app.extraction.detection import AudienceViewEventsDetector, run_detection
from app.extraction.inference.proposers.structured import AudienceViewEventsProposer
from app.extraction.inference.service import ConfigurationInferenceService
from app.extraction.normalize import normalize_candidate
from app.extraction.patterns.audienceview_events import (
    AudienceViewEventsPattern,
    find_audienceview_listing,
)
from app.extraction.registry import REGISTRY
from app.extraction.validate import validate_candidate
from app.schemas.extraction import SiteConfiguration
from tests.extraction_helpers import load_fixture, make_response

_FINAL_URL = (
    "https://hancher.uiowa.edu/Online/default.asp?"
    "BOparam::WScontent::loadArticle::permalink=upcoming-events"
)
_TZ = "America/Chicago"
_CONFIG = SiteConfiguration(
    pattern_name="audienceview_events",
    listing_url="https://hancher.uiowa.edu/upcoming-events",
    timezone=_TZ,
)


def _fixture_response():
    return make_response(load_fixture("audienceview_upcoming_events.html"), final_url=_FINAL_URL)


def _normalized(response, config=_CONFIG):
    candidates = AudienceViewEventsPattern().extract(response, config)
    return [normalize_candidate(c, config, fallback_timezone=_TZ) for c in candidates]


def _page(headers: list[str], rows: list[list], *, var: str = "articleContext") -> str:
    return (
        "<html><head><title>Upcoming</title></head><body>"
        f"<script>var {var} = {{  searchHeaders : {json.dumps(headers)}, "
        f"searchNames : [], searchResults : {json.dumps(rows)}, "
        "searchFilters : [ { name : 'f1', values : [] } ] };</script>"
        "</body></html>"
    )


_HEADERS = [
    "Id", "Object Type", "Description", "Short Description", "Start Date",
    "Start Date time", "Start Date date", "Start Date month", "Start Date year",
    "Data 1", "Data 2", "Venue Description", "Venue Data 10", "Image 1", "filter1",
]


def _row(
    id_: str,
    title: str,
    start: str = "Friday, March 5, 2027 / 7:30 PM",
    *,
    object_type: str = "P",
    link: str = '<a href="https://venue.example/show">MORE INFO</a>',
) -> list:
    return [
        id_, object_type, link, title, start, "19:30", "5", "2", "2027",
        title, "", "Main Hall", "1 Main St, Town, IA", "/img/a.jpg", "Music",
    ]


# --- The real page shape -----------------------------------------------------


def test_fixture_extracts_every_performance_with_all_fields():
    events = _normalized(_fixture_response())
    assert len(events) == 5
    assert all(validate_candidate(e, _CONFIG).is_valid for e in events)

    first = events[0]
    assert first.title == "Stella Cole Quartet"
    assert first.start_date.isoformat() == "2026-12-03"
    assert first.start_time.isoformat() == "19:00:00"
    assert first.canonical_url == "https://hancher.uiowa.edu/2627-stella-cole-quartet"
    assert first.venue == "Club Hancher - Strauss Hall"
    assert first.address == "141 E Park Rd, Iowa City, IA"
    assert first.description == "Merry Christmas, Darling"
    assert first.source_category == "Music"
    # The relative card image resolves against the page it came from.
    assert first.image_url == (
        "https://hancher.uiowa.edu/content/Images/2627Season/CardEventImages/SC_600x400.jpg"
    )
    assert first.external_source_id == "D7D23533-FF61-424A-9731-D2F45332F977"


def test_two_performances_of_one_show_stay_separate_events():
    # 7 PM and 9 PM sets share the show's info page; each performance id keeps
    # them apart, and within-run dedup must not collapse them.
    events = _normalized(_fixture_response())
    stella = [e for e in events if e.title == "Stella Cole Quartet"]
    assert [e.start_time.isoformat() for e in stella] == ["19:00:00", "21:00:00"]
    assert len({e.canonical_url for e in stella}) == 1
    outcome = dedupe_within_run(events, website_id=17, city_id=3)
    assert len(outcome.kept) == 5
    assert outcome.duplicates_skipped == 0


def test_javascript_apostrophe_escape_is_read():
    # The page writes Beethoven\'s, which is a JS escape but not valid JSON.
    titles = {e.title for e in _normalized(_fixture_response())}
    assert "UI Choir & Symphony Concert: Beethoven's 9th" in titles


def test_subtitle_lines_become_the_description():
    billy = next(e for e in _normalized(_fixture_response()) if e.title == "Billy Hart Quartet")
    assert billy.description == "ft. Ethan Iverson, Craig Handy & Ben Street"
    assert billy.start_date.isoformat() == "2027-01-23"


def test_extraction_is_deterministic():
    first = AudienceViewEventsPattern().extract(_fixture_response(), _CONFIG)
    second = AudienceViewEventsPattern().extract(_fixture_response(), _CONFIG)
    assert [c.raw_record_hash for c in first] == [c.raw_record_hash for c in second]
    assert [c.raw for c in first] == [c.raw for c in second]


# --- Detection, proposal and registry ----------------------------------------


def test_detector_claims_the_page_over_plain_http():
    result = AudienceViewEventsDetector().detect(_fixture_response())
    assert result.pattern_name == "audienceview_events"
    assert result.browser_required is False
    assert result.confidence >= 0.6
    assert result.evidence["performances"] == 5

    winner = run_detection(_fixture_response())
    assert winner.pattern_name == "audienceview_events"


def test_detector_ignores_pages_without_the_table():
    page = "<html><script>var x = { searchHeaders : ['a'] };</script></html>"
    result = AudienceViewEventsDetector().detect(make_response(page))
    assert result.pattern_name is None
    unrelated = _page(["Name", "Price"], [["Widget", "$1"]])
    assert AudienceViewEventsDetector().detect(make_response(unrelated)).pattern_name is None


def test_detector_reports_blocked_challenge_pages():
    blocked = make_response(
        "<html><title>Access denied</title>Are you a robot?</html>", status_code=403
    )
    assert AudienceViewEventsDetector().detect(blocked).pattern_name is None


def _context(response):
    return ConfigurationInferenceService(REGISTRY).build_context(
        response=response,
        detection=run_detection(response),
        listing_url="https://hancher.uiowa.edu/upcoming-events",
        fallback_timezone=_TZ,
    )


def test_proposer_builds_an_http_configuration():
    proposal = AudienceViewEventsProposer().propose(_context(_fixture_response()))
    config = proposal.configuration
    assert config is not None
    assert config.pattern_name == "audienceview_events"
    assert config.execution_strategy == "http"
    assert config.timezone == _TZ
    assert proposal.missing_required_fields == ()
    fields = {c.field: c for c in proposal.field_candidates}
    assert fields["title"].coverage == 1.0
    assert fields["start_datetime"].coverage == 1.0
    assert fields["canonical_url"].coverage == 1.0


def test_proposer_fails_cleanly_without_rows():
    page = make_response(_page(_HEADERS, []))
    proposal = AudienceViewEventsProposer().propose(_context(page))
    assert proposal.configuration is None


def test_pattern_is_registered_as_http_structured():
    registration = REGISTRY.get("audienceview_events")
    assert registration.browser_required is False
    assert registration.classification == "structured"
    assert registration.has_proposer
    # Below the vendor JSON APIs, above the generic inline-JSON and HTML resorts.
    assert registration.priority < REGISTRY.get("inline_json_events").priority
    assert registration.priority < REGISTRY.get("generic_html_cards").priority


# --- Missing and malformed data ----------------------------------------------


def test_missing_optional_fields_are_left_empty():
    headers = ["Id", "Short Description", "Start Date"]
    page = _page(headers, [["A1", "Quiet Recital", "Sunday, April 4, 2027"]])
    events = _normalized(make_response(page))
    assert len(events) == 1
    event = events[0]
    assert event.title == "Quiet Recital"
    assert event.start_date.isoformat() == "2027-04-04"
    assert event.start_time is None
    assert event.venue is None and event.address is None
    assert event.description is None and event.image_url is None
    assert event.canonical_url is None


def test_short_rows_and_non_list_rows_do_not_crash():
    page = _page(_HEADERS, [["B1", "P"], "not a row", None, _row("B2", "Full Row")])
    records = find_audienceview_listing(page).records
    assert [r["Id"] for r in records] == ["B1", "B2"]
    events = _normalized(make_response(page))
    assert events[0].title is None
    assert not validate_candidate(events[0], _CONFIG).is_valid
    assert validate_candidate(events[1], _CONFIG).is_valid


def test_split_date_columns_are_used_when_the_display_date_is_unreadable():
    # The split month column is zero-based: "2" is March.
    page = _page(_HEADERS, [_row("C1", "Fallback", start="TBA")])
    event = _normalized(make_response(page))[0]
    assert event.start_date.isoformat() == "2027-03-05"
    assert event.start_time.isoformat() == "19:30:00"


def test_unusable_dates_and_links_yield_no_value():
    row = _row("D1", "No Date", start="", link='<a href="javascript:void(0);">More</a>')
    row[6:9] = ["", "", ""]
    event = _normalized(make_response(_page(_HEADERS, [row])))[0]
    assert event.start_date is None
    assert event.canonical_url is None
    assert not validate_candidate(event, _CONFIG).is_valid


def test_repeated_performance_ids_are_extracted_once():
    page = _page(_HEADERS, [_row("E1", "Twice"), _row("E1", "Twice"), _row("E2", "Once")])
    candidates = AudienceViewEventsPattern().extract(make_response(page), _CONFIG)
    assert [c.raw["external_source_id"] for c in candidates] == ["E1", "E2"]


def test_packages_and_other_object_types_are_skipped():
    rows = [_row("F1", "Season package", object_type="S"), _row("F2", "Show")]
    candidates = AudienceViewEventsPattern().extract(make_response(_page(_HEADERS, rows)), _CONFIG)
    assert [c.raw["title"] for c in candidates] == ["Show"]


def test_invalid_table_literal_extracts_nothing():
    page = (
        "<script>var articleContext = { searchHeaders : [ \"Id\", \"Start Date\" ], "
        "searchResults : [ [ \"G1\", someFunction() ] ] };</script>"
    )
    assert find_audienceview_listing(page) is None
    assert AudienceViewEventsPattern().extract(make_response(page), _CONFIG) == []


def test_config_can_point_a_field_at_a_raw_column():
    config = _CONFIG.model_copy(update={"json_paths": {"description": "Venue Data 10"}})
    event = _normalized(_fixture_response(), config)[0]
    assert event.description == "141 E Park Rd, Iowa City, IA"
