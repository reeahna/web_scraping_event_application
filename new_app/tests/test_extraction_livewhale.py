import httpx
import pytest

from app.extraction.dedup import dedupe_within_run
from app.extraction.detection import MIN_PATTERN_CONFIDENCE, LiveWhaleDetector, run_detection
from app.extraction.normalize import normalize_candidate
from app.extraction.pagination import LiveWhaleOffsetPagination, build_pagination_strategy
from app.extraction.patterns.livewhale_json import LiveWhalePattern
from app.extraction.validate import validate_candidate
from app.models.event import Event
from app.models.event_provenance import EventProvenance
from app.schemas.extraction import SiteConfiguration
from app.services.extraction_runs import preview_extraction, run_extraction
from app.services.extraction_runs import run_detection as run_detection_service
from app.services.website_configuration import approve_configuration
from tests.extraction_helpers import (
    blocked_handler,
    html_handler,
    load_fixture,
    make_response,
    make_response_from_fixture,
    patched_http_fetch,
)

CONFIG = SiteConfiguration(
    pattern_name="livewhale_json",
    api_endpoint="https://example.edu/calendar/api/1/events",
    pagination={
        "strategy": "livewhale_offset",
        "page_param": "offset",
        "max_pages": 5,
        "max_events": 50,
    },
)


def _extract_page1():
    response = make_response_from_fixture(
        "livewhale_events_page1.json",
        final_url=CONFIG.api_endpoint,
        content_type="application/json",
    )
    return LiveWhalePattern().extract(response, CONFIG)


# --- Detection -----------------------------------------------------------------


def test_detector_matches_positive_fixture_with_high_confidence():
    response = make_response_from_fixture("livewhale_site_page.html")
    result = LiveWhaleDetector().detect(response)
    assert result.pattern_name == "livewhale_json"
    assert result.confidence >= MIN_PATTERN_CONFIDENCE
    assert not result.needs_review


def test_run_detection_selects_livewhale_on_positive_fixture():
    response = make_response_from_fixture("livewhale_site_page.html")
    result = run_detection(response)
    assert result.pattern_name == "livewhale_json"


def test_detector_discovers_the_api_endpoint():
    response = make_response_from_fixture("livewhale_site_page.html")
    result = LiveWhaleDetector().detect(response)
    assert result.discovered_endpoints == ("https://example.edu/calendar/api/1/events",)


def test_unrelated_wordpress_site_is_not_classified_as_livewhale():
    """No livewhale-specific evidence at all — must not match, and must
    never be inferred from the page's own URL string."""
    response = make_response_from_fixture("wordpress_site_page.html")
    result = LiveWhaleDetector().detect(response)
    assert result.pattern_name is None
    assert result.needs_review


def test_unsupported_page_does_not_match():
    response = make_response_from_fixture("unsupported_page.html")
    result = LiveWhaleDetector().detect(response)
    assert result.pattern_name is None


def test_direct_json_response_is_detected_by_shape():
    payload = (
        '{"events": [{"id": 1, "occur_id": "1-1", "title": "T", "date_ts": 1789480800}]}'
    )
    response = make_response(payload, content_type="application/json")
    result = LiveWhaleDetector().detect(response)
    assert result.pattern_name == "livewhale_json"
    assert result.confidence >= MIN_PATTERN_CONFIDENCE
    assert result.evidence.get("json_shape_match") is True


def test_blocked_response_never_produces_a_confident_match():
    response = make_response(
        "<html><body>Access Denied - please complete a CAPTCHA</body></html>",
        blocked_reason="http_403",
    )
    result = LiveWhaleDetector().detect(response)
    assert result.pattern_name is None
    assert result.needs_review


# --- Extraction: single page -----------------------------------------------------


def test_single_page_extraction_skips_malformed_and_keeps_valid_records():
    candidates = _extract_page1()
    # 6 array entries: 5 dicts + 1 non-dict (skipped entirely).
    assert len(candidates) == 5


def test_title_and_url_mapping():
    candidate = _extract_page1()[0]
    normalized = normalize_candidate(candidate, CONFIG)
    assert normalized.title == "Fall Concert Series"
    assert normalized.canonical_url == "https://example.edu/events/fall-concert"


def test_date_time_handling_timed_event():
    candidate = _extract_page1()[0]
    normalized = normalize_candidate(candidate, CONFIG)
    assert normalized.start_date.isoformat() == "2026-09-15"
    assert normalized.start_time.isoformat() == "14:00:00"
    assert normalized.end_date.isoformat() == "2026-09-15"
    assert normalized.end_time.isoformat() == "16:00:00"


def test_date_time_handling_all_day_event():
    candidate = _extract_page1()[2]  # All-Day Art Exhibit
    normalized = normalize_candidate(candidate, CONFIG)
    assert normalized.start_date.isoformat() == "2026-10-01"
    assert normalized.start_time is None


def test_invalid_date_rejected_not_crashed():
    candidate = _extract_page1()[4]  # Bad Date Event, date_ts="not-a-number"
    normalized = normalize_candidate(candidate, CONFIG)
    result = validate_candidate(normalized, CONFIG)
    assert not result.is_valid
    assert any("start date" in err for err in result.errors)
    assert any("unparseable_start_date" in w for w in normalized.warnings)


def test_html_sanitized_out_of_description():
    candidate = _extract_page1()[0]
    normalized = normalize_candidate(candidate, CONFIG)
    assert "<script>" not in normalized.description
    assert "javascript:" not in normalized.description
    assert "alert(1)" not in normalized.description
    assert "Full" in normalized.description and "HTML" in normalized.description
    # The pre-sanitized HTML is never promoted to the typed field or
    # persisted anywhere — only candidate.raw (in-memory, hashed, never
    # written to the database) retains it.
    assert "<script>" in candidate.raw["description"]


def test_missing_location_is_none_not_fabricated():
    candidate = _extract_page1()[1]  # Guest Lecture: no location/address/city/state/zip
    normalized = normalize_candidate(candidate, CONFIG)
    assert normalized.venue is None
    assert normalized.address is None


def test_venue_and_address_composed_from_parts():
    candidate = _extract_page1()[0]
    normalized = normalize_candidate(candidate, CONFIG)
    assert normalized.venue == "Recital Hall"
    assert normalized.address == "123 Campus Dr, Bloomington, IN, 47401"
    assert normalized.latitude == pytest.approx(39.16)
    assert normalized.longitude == pytest.approx(-86.52)


def test_image_mapping_plain_string():
    candidate = _extract_page1()[0]
    normalized = normalize_candidate(candidate, CONFIG)
    assert normalized.image_url == "https://example.edu/img/fall-concert.jpg"


def test_image_mapping_size_variant_dict():
    candidate = _extract_page1()[1]  # photo is {"lg": ..., "sm": ...}
    normalized = normalize_candidate(candidate, CONFIG)
    assert normalized.image_url == "https://example.edu/img/lecture-lg.jpg"


def test_group_mapped_to_source_category():
    candidate = _extract_page1()[0]
    normalized = normalize_candidate(candidate, CONFIG)
    assert normalized.source_category == "Music Department"


def test_missing_tags_and_groups_do_not_crash():
    candidate = _extract_page1()[1]  # tags=[], groups=[]
    assert candidate.raw["tags"] == []
    normalized = normalize_candidate(candidate, CONFIG)
    assert normalized.source_category is None


def test_contact_information_preserved_in_raw():
    candidate = _extract_page1()[0]
    assert candidate.raw["contact_name"] == "Jane Doe"
    assert candidate.raw["contact_email"] == "jane@example.edu"
    assert candidate.raw["contact_phone"] == "555-1234"


def test_summary_preserved_in_raw_not_promoted():
    candidate = _extract_page1()[0]
    assert candidate.raw["summary"] == "Short summary text"


def test_normalized_candidate_is_valid():
    candidate = _extract_page1()[0]
    normalized = normalize_candidate(candidate, CONFIG)
    result = validate_candidate(normalized, CONFIG)
    assert result.is_valid, result.errors


# --- Occurrence identity ----------------------------------------------------------


def test_occurrence_id_preferred_over_parent_id():
    candidate = _extract_page1()[0]  # id=101, occur_id="101-1"
    normalized = normalize_candidate(candidate, CONFIG)
    assert normalized.external_source_id == "101-1"
    assert candidate.raw["event_id"] == 101


def test_falls_back_to_parent_id_when_occur_id_absent():
    candidate = _extract_page1()[3]  # id=105, no occur_id key
    normalized = normalize_candidate(candidate, CONFIG)
    assert normalized.external_source_id == "105"


def test_duplicate_occurrence_not_inserted_twice():
    first = _extract_page1()
    again = _extract_page1()
    normalized = [normalize_candidate(c, CONFIG) for c in (first + again)]
    outcome = dedupe_within_run(normalized, website_id=1, city_id=None)
    assert len(outcome.kept) == 5
    assert outcome.duplicates_skipped == 5


# --- Provenance ----------------------------------------------------------------


def test_field_source_paths_use_livewhale_events_index_format():
    candidate = _extract_page1()[0]
    assert candidate.field_source_paths["title"] == "livewhale.events[0].title"
    assert candidate.field_source_paths["start_datetime"] == "livewhale.events[0].date_ts"
    assert candidate.field_source_paths["venue"] == "livewhale.events[0].location"


# --- Malformed / unexpected shapes ----------------------------------------------


def test_invalid_json_produces_zero_candidates_not_a_crash():
    response = make_response_from_fixture(
        "livewhale_events_malformed.json",
        final_url=CONFIG.api_endpoint,
        content_type="application/json",
    )
    assert LiveWhalePattern().extract(response, CONFIG) == []


def test_unexpected_response_shape_produces_zero_candidates():
    response = make_response_from_fixture(
        "livewhale_events_unexpected_shape.json",
        final_url=CONFIG.api_endpoint,
        content_type="application/json",
    )
    assert LiveWhalePattern().extract(response, CONFIG) == []


def test_bare_list_payload_also_accepted():
    response = make_response(
        '[{"id": 9, "occur_id": "9-1", "title": "Bare list event", '
        '"url": "https://example.edu/e/9", "date_ts": 1789480800}]',
        final_url=CONFIG.api_endpoint,
        content_type="application/json",
    )
    candidates = LiveWhalePattern().extract(response, CONFIG)
    assert len(candidates) == 1
    assert candidates[0].raw["title"] == "Bare list event"


# --- Pagination ------------------------------------------------------------------


def test_pagination_computes_next_offset_from_event_count():
    response = make_response_from_fixture(
        "livewhale_events_page1.json",
        final_url="https://example.edu/calendar/api/1/events?offset=0",
        content_type="application/json",
    )
    result = LiveWhaleOffsetPagination().next_request(
        response, 0, CONFIG, visited_urls=frozenset(), seen_body_hashes=frozenset()
    )
    assert result is not None
    # 6 array entries (including the malformed one, which still counts as a
    # returned result at the pagination layer — it operates on the JSON
    # array, before the pattern's own dict-type filtering).
    assert result.url == "https://example.edu/calendar/api/1/events?offset=6"


def test_pagination_defaults_offset_to_zero_when_param_absent():
    response = make_response_from_fixture(
        "livewhale_events_page1.json",
        final_url="https://example.edu/calendar/api/1/events",
        content_type="application/json",
    )
    result = LiveWhaleOffsetPagination().next_request(
        response, 0, CONFIG, visited_urls=frozenset(), seen_body_hashes=frozenset()
    )
    assert result is not None
    assert "offset=6" in result.url


def test_pagination_stops_on_empty_results():
    response = make_response_from_fixture(
        "livewhale_events_page2.json",
        final_url="https://example.edu/calendar/api/1/events?offset=6",
        content_type="application/json",
    )
    result = LiveWhaleOffsetPagination().next_request(
        response, 1, CONFIG, visited_urls=frozenset(), seen_body_hashes=frozenset()
    )
    assert result is None


def test_pagination_respects_max_pages():
    response = make_response_from_fixture(
        "livewhale_events_page1.json",
        final_url="https://example.edu/calendar/api/1/events?offset=0",
        content_type="application/json",
    )
    config = CONFIG.model_copy(
        update={"pagination": CONFIG.pagination.model_copy(update={"max_pages": 1})}
    )
    result = LiveWhaleOffsetPagination().next_request(
        response, 0, config, visited_urls=frozenset(), seen_body_hashes=frozenset()
    )
    assert result is None


def test_build_pagination_strategy_dispatches_livewhale_offset():
    assert isinstance(build_pagination_strategy(CONFIG), LiveWhaleOffsetPagination)


# --- Group/tag filtering reach the real request -----------------------------------


@pytest.mark.asyncio
async def test_configured_group_and_tag_filters_are_sent_on_the_request(
    db_session, make_city, make_website
):
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.url.params))
        body = load_fixture("livewhale_events_page1.json")
        return httpx.Response(200, text=body, headers={"content-type": "application/json"})

    # pagination "none": this test asserts filters reach the request that's
    # actually made, not that they survive a second reconstructed-URL page
    # (see LiveWhaleOffsetPagination's docstring/module notes — query params
    # passed separately to the first request aren't part of
    # FetchResponse.final_url, so no manually-rebuilt-URL strategy in this
    # module carries them past page one; a pre-existing characteristic
    # shared with query_param/wordpress, not something new here).
    filtered_config = CONFIG.model_copy(
        update={
            "pagination": CONFIG.pagination.model_copy(update={"strategy": "none"}),
            "fetch": CONFIG.fetch.model_copy(
                update={"query_params": {"group": "music-department", "tag": "free"}}
            ),
        }
    )

    city = make_city()
    website = make_website(city, name="LiveWhale Test Source", base_url="https://example.edu")
    website.configuration = filtered_config.model_dump(mode="json")
    db_session.commit()

    with patched_http_fetch(handler):
        result = await preview_extraction(db_session, website)

    assert result.status in ("success", "partial")
    assert captured.get("group") == "music-department"
    assert captured.get("tag") == "free"


# --- Preview: never persists Event rows -----------------------------------------


@pytest.fixture
def website_with_config(make_city, make_website):
    city = make_city()
    website = make_website(city, name="LiveWhale Preview Source", base_url="https://example.edu")
    website.configuration = CONFIG.model_dump(mode="json")
    return website


@pytest.mark.asyncio
async def test_preview_saves_no_event_rows(db_session, website_with_config):
    website = website_with_config
    db_session.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        if "offset=6" in str(request.url):
            body = load_fixture("livewhale_events_page2.json")
        else:
            body = load_fixture("livewhale_events_page1.json")
        return httpx.Response(200, text=body, headers={"content-type": "application/json"})

    with patched_http_fetch(handler):
        result = await preview_extraction(db_session, website)

    assert result.status in ("success", "partial")
    assert result.events_found == 5
    assert result.events_inserted == 0
    assert db_session.query(Event).count() == 0
    assert db_session.query(EventProvenance).count() == 0


@pytest.mark.asyncio
async def test_preview_reports_blocked_status_without_crashing(db_session, website_with_config):
    website = website_with_config
    db_session.commit()

    with patched_http_fetch(blocked_handler(403)):
        result = await preview_extraction(db_session, website)

    assert result.status == "blocked"
    assert result.events_inserted == 0
    assert db_session.query(Event).count() == 0


# --- Bounded end-to-end fixture verification ------------------------------------
# detection -> configuration -> preview -> approval -> activation ->
# persistent extraction -> repeat extraction, entirely against local
# fixtures (no live external source).


def _livewhale_paginated_handler():
    page1 = load_fixture("livewhale_events_page1.json")
    page2 = load_fixture("livewhale_events_page2.json")

    def handler(request: httpx.Request) -> httpx.Response:
        body = page2 if "offset=6" in str(request.url) else page1
        return httpx.Response(200, text=body, headers={"content-type": "application/json"})

    return handler


@pytest.mark.asyncio
async def test_full_workflow_detection_through_repeat_extraction(
    db_session, make_city, make_website, make_user
):
    city = make_city()
    website = make_website(city, name="LiveWhale E2E Source", base_url="https://example.edu")
    website.event_listing_url = "https://example.edu/calendar"
    db_session.commit()

    # 1. Detection against the site's HTML.
    with patched_http_fetch(html_handler("livewhale_site_page.html")):
        detection_result = await run_detection_service(db_session, website)
    assert detection_result.status == "success"
    assert detection_result.pattern == "livewhale_json"
    discovered = website.proposed_pattern["detection"]["discovered_endpoints"]
    assert discovered == ["https://example.edu/calendar/api/1/events"]

    # 2. Configuration: adopt the proposed draft (mirrors what an admin
    # would save from the review screen).
    website.configuration = CONFIG.model_dump(mode="json")
    db_session.commit()

    # 3. Preview. 5 well-formed event dicts, 1 of which (Bad Date Event) has
    # an unparseable date_ts and is correctly rejected, not guessed at.
    with patched_http_fetch(_livewhale_paginated_handler()):
        preview_result = await preview_extraction(db_session, website)
    assert preview_result.status == "partial"
    assert preview_result.events_found == 5
    assert preview_result.events_valid == 4

    # 4. Approval.
    admin = make_user(email="livewhale-approver@example.com")
    approve_configuration(db_session, website, approved_by_user_id=admin.id)
    assert website.approved_pattern is not None

    # 5. Activation.
    website.onboarding_status = "active"
    website.is_active = True
    db_session.commit()

    # 6. Persistent extraction.
    with patched_http_fetch(_livewhale_paginated_handler()):
        run_result = await run_extraction(db_session, website, triggered_by_user_id=admin.id)
    assert run_result.status == "partial"
    assert run_result.events_inserted == 4
    assert db_session.query(Event).count() == 4
    assert db_session.query(EventProvenance).count() == 4

    # 7. Repeat extraction: upserts, never duplicates.
    with patched_http_fetch(_livewhale_paginated_handler()):
        repeat_result = await run_extraction(db_session, website, triggered_by_user_id=admin.id)
    assert repeat_result.events_inserted == 0
    assert repeat_result.events_updated == 4
    assert db_session.query(Event).count() == 4
