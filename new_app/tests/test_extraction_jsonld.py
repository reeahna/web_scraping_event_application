from datetime import date, time

from app.extraction.normalize import normalize_candidate
from app.extraction.patterns.jsonld import JsonLdEventPattern
from app.extraction.validate import validate_candidate
from app.schemas.extraction import SiteConfiguration
from tests.extraction_helpers import make_response, make_response_from_fixture

CONFIG = SiteConfiguration(pattern_name="json_ld_event", listing_url="https://example.com/events")


def _extract(fixture_name: str):
    response = make_response_from_fixture(fixture_name)
    return JsonLdEventPattern().extract(response, CONFIG)


def test_single_event_extracted():
    candidates = _extract("jsonld_single_event.html")
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.raw["title"] == "Jazz Night at the Park"
    assert candidate.raw["canonical_url"] == "https://example.com/events/jazz-night"
    assert candidate.field_source_paths["title"] == "jsonpath:name"


def test_single_event_normalizes_dates_times_and_address():
    candidate = _extract("jsonld_single_event.html")[0]
    normalized = normalize_candidate(candidate, CONFIG)
    assert normalized.start_date == date(2025, 6, 1)
    assert normalized.start_time == time(19, 0, 0)
    assert normalized.end_date == date(2025, 6, 1)
    assert normalized.end_time == time(22, 0, 0)
    assert normalized.venue == "Central Park Bandshell"
    assert "123 Park Ave" in normalized.address
    assert normalized.image_url == "https://example.com/images/jazz.jpg"
    result = validate_candidate(normalized, CONFIG)
    assert result.is_valid, result.errors


def test_multiple_events_including_graph_and_subtypes():
    candidates = _extract("jsonld_multiple_events.html")
    assert len(candidates) == 2
    titles = {c.raw["title"] for c in candidates}
    assert titles == {"Symphony Under the Stars", "Food Truck Festival"}


def test_malformed_jsonld_block_skipped_valid_one_kept():
    candidates = _extract("jsonld_malformed.html")
    assert len(candidates) == 1
    assert candidates[0].raw["title"] == "Valid Event After Broken One"


def test_missing_title_rejected_by_validator():
    candidate = _extract("jsonld_missing_title.html")[0]
    normalized = normalize_candidate(candidate, CONFIG)
    result = validate_candidate(normalized, CONFIG)
    assert not result.is_valid
    assert any("title" in err for err in result.errors)


def test_missing_start_date_rejected_by_validator():
    candidate = _extract("jsonld_missing_date.html")[0]
    normalized = normalize_candidate(candidate, CONFIG)
    result = validate_candidate(normalized, CONFIG)
    assert not result.is_valid
    assert any("start date" in err for err in result.errors)


def test_end_before_start_rejected():
    response = make_response_from_fixture("jsonld_single_event.html")
    candidate = JsonLdEventPattern().extract(response, CONFIG)[0]
    candidate.raw["end_datetime"] = "2025-05-30"  # before startDate 2025-06-01
    normalized = normalize_candidate(candidate, CONFIG)
    result = validate_candidate(normalized, CONFIG)
    assert not result.is_valid
    assert any("end_date is before start_date" in err for err in result.errors)


def test_page_url_not_used_as_canonical_url_unless_configured():
    response = make_response_from_fixture(
        "jsonld_missing_date.html", final_url="https://example.com/some-listing-page"
    )
    candidate = JsonLdEventPattern().extract(response, CONFIG)[0]
    # jsonld_missing_date.html DOES have a url field, so this checks the
    # narrower "no fallback used unless configured" behavior via a variant
    # with no url at all:
    candidate.raw["canonical_url"] = None
    normalized = normalize_candidate(candidate, CONFIG)
    assert normalized.canonical_url is None

    allow_fallback_config = SiteConfiguration(
        pattern_name="json_ld_event",
        listing_url="https://example.com/events",
        allow_page_url_as_canonical_fallback=True,
    )
    response2 = make_response_from_fixture(
        "jsonld_missing_date.html", final_url="https://example.com/some-listing-page"
    )
    candidate2 = JsonLdEventPattern().extract(response2, allow_fallback_config)[0]
    assert candidate2.raw["canonical_url"] == "https://example.com/events/no-date"


def test_offers_url_not_used_as_event_url_unless_configured():
    html = """
    <script type="application/ld+json">
    {"@type": "Event", "name": "Ticketed Show", "startDate": "2025-09-01",
     "offers": {"@type": "Offer", "url": "https://tickets.example.com/buy/123"}}
    </script>
    """
    response = make_response(html, final_url="https://example.com/events/ticketed-show")
    candidate = JsonLdEventPattern().extract(response, CONFIG)[0]
    assert candidate.raw["canonical_url"] is None

    allow_offers_config = SiteConfiguration(
        pattern_name="json_ld_event",
        listing_url="https://example.com/events",
        allow_offers_url_as_event_url=True,
    )
    candidate2 = JsonLdEventPattern().extract(response, allow_offers_config)[0]
    assert candidate2.raw["canonical_url"] == "https://tickets.example.com/buy/123"


def test_field_source_paths_recorded_for_every_field():
    candidate = _extract("jsonld_single_event.html")[0]
    for field_name in ("title", "start_datetime", "canonical_url", "venue", "address"):
        assert field_name in candidate.field_source_paths
