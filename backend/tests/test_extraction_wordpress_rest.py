from app.extraction.normalize import normalize_candidate
from app.extraction.patterns.wordpress_rest import WordPressRestPattern
from app.extraction.validate import validate_candidate
from app.schemas.extraction import SiteConfiguration
from tests.extraction_helpers import make_response, make_response_from_fixture

CONFIG = SiteConfiguration(
    pattern_name="wordpress_rest",
    api_endpoint="https://example.com/wp-json/wp/v2/tribe_events",
)


def test_wordpress_posts_extracted_and_mapped():
    response = make_response_from_fixture(
        "wordpress_rest_page1.json",
        final_url="https://example.com/wp-json/wp/v2/tribe_events",
        content_type="application/json",
    )
    candidates = WordPressRestPattern().extract(response, CONFIG)
    assert len(candidates) == 2
    assert candidates[0].raw["title"] == "Summer Concert Series"
    assert candidates[0].raw["canonical_url"] == "https://example.com/events/summer-concert"
    assert candidates[0].raw["external_source_id"] == "101"


def test_configurable_post_type_and_custom_date_field():
    payload = (
        '[{"id": 5, "custom_start": "2025-09-10", "title": {"rendered": "Custom Field Event"}, '
        '"link": "https://example.com/events/custom"}]'
    )
    config = SiteConfiguration(
        pattern_name="wordpress_rest",
        api_endpoint="https://example.com/wp-json/wp/v2/custom_post_type",
        json_paths={"start_datetime": "custom_start"},
    )
    response = make_response(
        payload, final_url=config.api_endpoint, content_type="application/json"
    )
    candidate = WordPressRestPattern().extract(response, config)[0]
    assert candidate.raw["start_datetime"] == "2025-09-10"


def test_normalized_and_validated_wordpress_event():
    response = make_response_from_fixture(
        "wordpress_rest_page1.json",
        final_url="https://example.com/wp-json/wp/v2/tribe_events",
        content_type="application/json",
    )
    candidate = WordPressRestPattern().extract(response, CONFIG)[0]
    normalized = normalize_candidate(candidate, CONFIG)
    result = validate_candidate(normalized, CONFIG)
    assert result.is_valid, result.errors
    assert normalized.description == "Join us for an evening of music."  # HTML stripped


def test_malformed_json_produces_zero_candidates_not_a_crash():
    response = make_response_from_fixture(
        "wordpress_rest_malformed.json",
        final_url="https://example.com/wp-json/wp/v2/tribe_events",
        content_type="application/json",
    )
    candidates = WordPressRestPattern().extract(response, CONFIG)
    assert candidates == []


def test_non_event_post_rejected_when_no_date_mapping_configured():
    payload = '[{"id": 9, "title": {"rendered": "Just a blog post"}, "link": "https://example.com/blog/post"}]'
    config = SiteConfiguration(
        pattern_name="wordpress_rest",
        api_endpoint="https://example.com/wp-json/wp/v2/posts",
        json_paths={"start_datetime": "nonexistent_field"},
    )
    response = make_response(
        payload, final_url=config.api_endpoint, content_type="application/json"
    )
    candidate = WordPressRestPattern().extract(response, config)[0]
    normalized = normalize_candidate(candidate, config)
    result = validate_candidate(normalized, config)
    assert not result.is_valid
    assert any("start date" in err for err in result.errors)


def test_field_source_paths_recorded():
    response = make_response_from_fixture(
        "wordpress_rest_page1.json",
        final_url="https://example.com/wp-json/wp/v2/tribe_events",
        content_type="application/json",
    )
    candidate = WordPressRestPattern().extract(response, CONFIG)[0]
    assert candidate.field_source_paths["title"] == "jsonpath:title.rendered"
    assert candidate.field_source_paths["canonical_url"] == "jsonpath:link"
