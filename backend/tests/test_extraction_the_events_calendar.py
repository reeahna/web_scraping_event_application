from datetime import date

import httpx
import pytest

from app.extraction.dedup import dedupe_within_run
from app.extraction.detection import (
    MIN_PATTERN_CONFIDENCE,
    TheEventsCalendarDetector,
    run_detection,
)
from app.extraction.normalize import normalize_candidate
from app.extraction.pagination import TribeRestPagination, build_pagination_strategy
from app.extraction.patterns.the_events_calendar import TheEventsCalendarPattern
from app.extraction.validate import validate_candidate
from app.models.event import Event
from app.models.event_provenance import EventProvenance
from app.schemas.extraction import SiteConfiguration
from app.services.extraction_runs import preview_extraction, run_extraction
from app.services.extraction_runs import run_detection as run_detection_service
from app.services.website_configuration import approve_configuration
from tests.extraction_helpers import (
    html_handler,
    load_fixture,
    make_response,
    make_response_from_fixture,
    patched_http_fetch,
)

CONFIG = SiteConfiguration(
    pattern_name="the_events_calendar",
    api_endpoint="https://example.com/wp-json/tribe/events/v1/events",
    pagination={"strategy": "tribe_rest", "max_pages": 5, "max_events": 50},
)


def _extract_page1():
    response = make_response_from_fixture(
        "tribe_events_page1.json",
        final_url=CONFIG.api_endpoint,
        content_type="application/json",
    )
    return TheEventsCalendarPattern().extract(response, CONFIG)


# --- Detection ---------------------------------------------------------------


def test_detector_matches_positive_fixture_with_high_confidence():
    response = make_response_from_fixture("tribe_site_page.html")
    result = TheEventsCalendarDetector().detect(response)
    assert result.pattern_name == "the_events_calendar"
    assert result.confidence >= MIN_PATTERN_CONFIDENCE
    assert not result.needs_review


def test_run_detection_prefers_the_events_calendar_over_generic_wordpress():
    response = make_response_from_fixture("tribe_site_page.html")
    result = run_detection(response)
    assert result.pattern_name == "the_events_calendar"


def test_plain_wordpress_site_is_not_classified_as_the_events_calendar():
    """A WordPress site with no tribe-specific evidence must not match —
    otherwise every WordPress site would be misclassified."""
    response = make_response_from_fixture("wordpress_site_page.html")
    result = TheEventsCalendarDetector().detect(response)
    assert result.pattern_name is None
    assert result.needs_review


def test_unsupported_page_does_not_match():
    response = make_response_from_fixture("unsupported_page.html")
    result = TheEventsCalendarDetector().detect(response)
    assert result.pattern_name is None


def test_detector_discovers_the_listing_rest_endpoint():
    response = make_response_from_fixture("tribe_site_page.html")
    result = TheEventsCalendarDetector().detect(response)
    assert result.discovered_endpoints == ("https://example.com/wp-json/tribe/events/v1/events",)


def test_blocked_response_never_produces_a_confident_match():
    response = make_response(
        "<html><body>Access Denied - please complete a CAPTCHA</body></html>",
        blocked_reason="http_403",
    )
    result = TheEventsCalendarDetector().detect(response)
    assert result.pattern_name is None
    assert result.needs_review


# --- Extraction: single page ---------------------------------------------------


def test_single_page_extraction_returns_all_events():
    candidates = _extract_page1()
    assert len(candidates) == 2


def test_title_and_url_mapping():
    candidate = _extract_page1()[0]
    normalized = normalize_candidate(candidate, CONFIG)
    assert normalized.title == "Summer Jazz Night"
    assert normalized.canonical_url == "https://example.com/event/summer-jazz-night/"


def test_venue_mapping_composes_full_address():
    candidate = _extract_page1()[0]
    normalized = normalize_candidate(candidate, CONFIG)
    assert normalized.venue == "The Blue Note"
    assert normalized.address == "123 Main St, Bloomington, IN, 47401"
    assert normalized.latitude == pytest.approx(39.1653)
    assert normalized.longitude == pytest.approx(-86.5264)
    # Individual components are preserved for provenance even though only
    # the composed string is a typed field.
    assert candidate.raw["locality"] == "Bloomington"
    assert candidate.raw["region"] == "IN"
    assert candidate.raw["postal_code"] == "47401"


def test_organizer_mapping():
    candidate = _extract_page1()[0]
    assert candidate.raw["organizer"] == "Downtown Events Co"


def test_image_mapping():
    candidate = _extract_page1()[0]
    normalized = normalize_candidate(candidate, CONFIG)
    assert normalized.image_url == "https://example.com/wp-content/uploads/jazz.jpg"


def test_image_false_resolves_to_none_not_a_literal_false():
    candidate = _extract_page1()[1]
    assert candidate.raw["image"] is None


def test_all_day_event_has_no_start_time():
    candidate = _extract_page1()[1]  # Farmers Market, all_day: true
    normalized = normalize_candidate(candidate, CONFIG)
    assert normalized.start_date == date(2026, 8, 2)
    assert normalized.start_time is None


def test_timed_event_keeps_start_time():
    candidate = _extract_page1()[0]  # Summer Jazz Night, all_day: false
    normalized = normalize_candidate(candidate, CONFIG)
    assert normalized.start_date == date(2026, 8, 1)
    assert normalized.start_time is not None
    assert normalized.start_time.isoformat() == "19:00:00"


def test_missing_optional_fields_are_none_not_fabricated():
    candidate = _extract_page1()[1]  # Farmers Market: no organizer, no categories
    assert candidate.raw["organizer"] is None
    assert candidate.raw["source_category"] is None
    assert candidate.raw["series_id"] is None
    assert candidate.raw["recurrence"] is None


def test_timezone_uses_config_or_fallback_never_the_raw_source_value():
    """Matches every other pattern's normalizer contract: only
    config.timezone / the caller's fallback_timezone ever become
    candidate.timezone — a source-provided timezone string is preserved in
    `raw` for provenance only, never silently substituted by the shared
    normalizer (that logic lives in exactly one place, not per-pattern)."""
    candidate = _extract_page1()[0]
    assert candidate.raw["timezone"] == "America/Indiana/Indianapolis"

    normalized = normalize_candidate(candidate, CONFIG, fallback_timezone="America/New_York")
    assert normalized.timezone == "America/New_York"

    config_with_tz = CONFIG.model_copy(update={"timezone": "America/Chicago"})
    normalized_override = normalize_candidate(candidate, config_with_tz)
    assert normalized_override.timezone == "America/Chicago"


def test_normalized_candidate_is_valid():
    candidate = _extract_page1()[0]
    normalized = normalize_candidate(candidate, CONFIG)
    result = validate_candidate(normalized, CONFIG)
    assert result.is_valid, result.errors


# --- Provenance ----------------------------------------------------------------


def test_field_source_paths_recorded_in_tribe_events_format():
    candidate = _extract_page1()[0]
    assert candidate.field_source_paths["title"] == "tribe.events[0].title"
    assert candidate.field_source_paths["start_datetime"] == "tribe.events[0].start_date"
    assert candidate.field_source_paths["address"] == "tribe.events[0].venue.address"


def test_field_source_paths_index_reflects_position_in_page():
    candidate = _extract_page1()[1]
    assert candidate.field_source_paths["title"] == "tribe.events[1].title"


# --- Recurrence / occurrence identity -------------------------------------------


def test_explicit_recurrence_metadata_preserved_verbatim():
    response = make_response_from_fixture(
        "tribe_events_page2.json", final_url=CONFIG.api_endpoint, content_type="application/json"
    )
    candidate = TheEventsCalendarPattern().extract(response, CONFIG)[0]
    assert candidate.raw["recurrence"] == {"rules": [{"type": "Weekly", "interval": 1}]}


def test_explicit_occurrence_and_series_id_preserved():
    response = make_response_from_fixture(
        "tribe_events_page2.json", final_url=CONFIG.api_endpoint, content_type="application/json"
    )
    candidate = TheEventsCalendarPattern().extract(response, CONFIG)[0]
    normalized = normalize_candidate(candidate, CONFIG)
    assert normalized.external_source_id == "503"  # the occurrence's own stable id
    assert candidate.raw["series_id"] == "trivia-weekly"


def test_duplicate_occurrence_not_inserted_twice():
    """Same occurrence id appearing twice (e.g. an overlapping page refetch)
    collapses via the existing external-source-id fingerprint — no
    pattern-specific dedup logic needed."""
    first = _extract_page1()
    again = _extract_page1()
    normalized = [normalize_candidate(c, CONFIG) for c in (first + again)]
    outcome = dedupe_within_run(normalized, website_id=1, city_id=None)
    assert len(outcome.kept) == 2
    assert outcome.duplicates_skipped == 2


# --- Malformed / unexpected shapes ----------------------------------------------


def test_invalid_json_produces_zero_candidates_not_a_crash():
    response = make_response_from_fixture(
        "tribe_events_malformed.json",
        final_url=CONFIG.api_endpoint,
        content_type="application/json",
    )
    assert TheEventsCalendarPattern().extract(response, CONFIG) == []


def test_unsupported_response_shape_produces_zero_candidates():
    response = make_response_from_fixture(
        "tribe_events_unexpected_shape.json",
        final_url=CONFIG.api_endpoint,
        content_type="application/json",
    )
    assert TheEventsCalendarPattern().extract(response, CONFIG) == []


def test_one_malformed_event_record_does_not_abort_the_run():
    response = make_response_from_fixture(
        "tribe_events_with_invalid_record.json",
        final_url=CONFIG.api_endpoint,
        content_type="application/json",
    )
    candidates = TheEventsCalendarPattern().extract(response, CONFIG)
    # The non-dict entry is skipped; both real event dicts still produce candidates.
    assert len(candidates) == 2

    valid = normalize_candidate(candidates[0], CONFIG)
    invalid = normalize_candidate(candidates[1], CONFIG)
    assert validate_candidate(valid, CONFIG).is_valid
    result = validate_candidate(invalid, CONFIG)
    assert not result.is_valid
    assert any("start date" in err for err in result.errors)


def test_bare_list_payload_also_accepted():
    response = make_response(
        '[{"id": 9, "title": "Bare list event", "url": "https://example.com/e/9", '
        '"start_date": "2026-10-01 12:00:00"}]',
        final_url=CONFIG.api_endpoint,
        content_type="application/json",
    )
    candidates = TheEventsCalendarPattern().extract(response, CONFIG)
    assert len(candidates) == 1
    assert candidates[0].raw["title"] == "Bare list event"


# --- Pagination ------------------------------------------------------------------


def test_pagination_follows_next_rest_url():
    response = make_response_from_fixture(
        "tribe_events_page1.json", final_url=CONFIG.api_endpoint, content_type="application/json"
    )
    result = TribeRestPagination().next_request(
        response, 0, CONFIG, visited_urls=frozenset(), seen_body_hashes=frozenset()
    )
    assert result is not None
    assert result.url == "https://example.com/wp-json/tribe/events/v1/events?page=2"


def test_pagination_stops_when_next_rest_url_is_null():
    response = make_response_from_fixture(
        "tribe_events_page2.json",
        final_url="https://example.com/wp-json/tribe/events/v1/events?page=2",
        content_type="application/json",
    )
    result = TribeRestPagination().next_request(
        response, 1, CONFIG, visited_urls=frozenset(), seen_body_hashes=frozenset()
    )
    assert result is None


def test_pagination_respects_max_pages():
    response = make_response_from_fixture(
        "tribe_events_page1.json", final_url=CONFIG.api_endpoint, content_type="application/json"
    )
    config = CONFIG.model_copy(
        update={"pagination": CONFIG.pagination.model_copy(update={"max_pages": 1})}
    )
    result = TribeRestPagination().next_request(
        response, 0, config, visited_urls=frozenset(), seen_body_hashes=frozenset()
    )
    assert result is None


def test_build_pagination_strategy_dispatches_tribe_rest():
    assert isinstance(build_pagination_strategy(CONFIG), TribeRestPagination)


def _tribe_paginated_handler():
    page1 = load_fixture("tribe_events_page1.json")
    page2 = load_fixture("tribe_events_page2.json")

    def handler(request: httpx.Request) -> httpx.Response:
        body = page2 if "page=2" in str(request.url) else page1
        return httpx.Response(200, text=body, headers={"content-type": "application/json"})

    return handler


# --- Preview: never persists Event rows -----------------------------------------


@pytest.fixture
def website_with_config(make_city, make_website):
    city = make_city()
    website = make_website(city, name="Tribe Test Source", base_url="https://example.com")
    website.configuration = CONFIG.model_dump(mode="json")
    return website


@pytest.mark.asyncio
async def test_preview_saves_no_event_rows(db_session, website_with_config):
    website = website_with_config
    db_session.commit()

    with patched_http_fetch(_tribe_paginated_handler()):
        result = await preview_extraction(db_session, website)

    assert result.status == "success"
    assert result.events_found == 3  # 2 on page 1 + 1 on page 2
    assert result.events_valid == 3
    assert result.events_inserted == 0
    assert db_session.query(Event).count() == 0
    assert db_session.query(EventProvenance).count() == 0


# --- Bounded end-to-end fixture verification ------------------------------------
# detection -> configuration -> preview -> approval -> activation ->
# persistent extraction -> repeat extraction, entirely against local fixtures.


@pytest.mark.asyncio
async def test_full_workflow_detection_through_repeat_extraction(
    db_session, make_city, make_website, make_user
):
    city = make_city()
    website = make_website(city, name="Tribe E2E Source", base_url="https://example.com")
    website.event_listing_url = "https://example.com/"
    db_session.commit()

    # 1. Detection against the site's HTML.
    with patched_http_fetch(html_handler("tribe_site_page.html")):
        detection_result = await run_detection_service(db_session, website)
    assert detection_result.status == "success"
    assert detection_result.pattern == "the_events_calendar"
    discovered = website.proposed_pattern["detection"]["discovered_endpoints"]
    assert discovered == ["https://example.com/wp-json/tribe/events/v1/events"]

    # 2. Configuration: adopt the proposed draft (mirrors what an admin
    # would save from the review screen).
    website.configuration = CONFIG.model_dump(mode="json")
    db_session.commit()

    # 3. Preview.
    with patched_http_fetch(_tribe_paginated_handler()):
        preview_result = await preview_extraction(db_session, website)
    assert preview_result.status == "success"
    assert preview_result.events_valid == 3

    # 4. Approval.
    admin = make_user(email="tribe-approver@example.com")
    approve_configuration(db_session, website, approved_by_user_id=admin.id)
    assert website.approved_pattern is not None

    # 5. Activation.
    website.onboarding_status = "active"
    website.is_active = True
    db_session.commit()

    # 6. Persistent extraction.
    with patched_http_fetch(_tribe_paginated_handler()):
        run_result = await run_extraction(db_session, website, triggered_by_user_id=admin.id)
    assert run_result.status == "success"
    assert run_result.events_inserted == 3
    assert db_session.query(Event).count() == 3
    assert db_session.query(EventProvenance).count() == 3

    # 7. Repeat extraction: upserts, never duplicates.
    with patched_http_fetch(_tribe_paginated_handler()):
        repeat_result = await run_extraction(db_session, website, triggered_by_user_id=admin.id)
    assert repeat_result.status == "success"
    assert repeat_result.events_inserted == 0
    assert repeat_result.events_updated == 3
    assert db_session.query(Event).count() == 3
