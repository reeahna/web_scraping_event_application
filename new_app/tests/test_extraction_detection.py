from app.extraction.detection import (
    MIN_PATTERN_CONFIDENCE,
    JsonLdDetector,
    StaticHtmlDetector,
    WordPressRestDetector,
    run_detection,
)
from tests.extraction_helpers import make_response, make_response_from_fixture


def test_jsonld_detector_matches_single_event_fixture():
    response = make_response_from_fixture("jsonld_single_event.html")
    result = JsonLdDetector().detect(response)
    assert result.pattern_name == "json_ld_event"
    assert result.confidence >= MIN_PATTERN_CONFIDENCE
    assert not result.needs_review


def test_static_html_detector_matches_cards_fixture():
    response = make_response_from_fixture("static_html_cards.html")
    result = StaticHtmlDetector().detect(response)
    assert result.pattern_name == "generic_html_cards"
    assert "container_selector_candidate" in result.evidence


def test_wordpress_detector_matches_wordpress_fixture():
    response = make_response_from_fixture("wordpress_site_page.html")
    result = WordPressRestDetector().detect(response)
    assert result.pattern_name == "wordpress_rest"
    assert result.discovered_endpoints == ("https://example.com/wp-json/",)


def test_unsupported_page_matches_nothing():
    response = make_response_from_fixture("unsupported_page.html")
    for detector in (JsonLdDetector(), StaticHtmlDetector(), WordPressRestDetector()):
        result = detector.detect(response)
        assert result.pattern_name is None
        assert result.needs_review


def test_run_detection_picks_highest_confidence_with_reliability_tiebreak():
    response = make_response_from_fixture("wordpress_site_page.html")
    result = run_detection(response)
    # wordpress_site_page.html only has WordPress signals — only that
    # detector should win regardless of tie-break order.
    assert result.pattern_name == "wordpress_rest"


def test_run_detection_returns_unsupported_when_nothing_matches():
    response = make_response_from_fixture("unsupported_page.html")
    result = run_detection(response)
    assert result.pattern_name is None
    assert result.needs_review


def test_below_threshold_confidence_never_silently_accepted():
    response = make_response_from_fixture("unsupported_page.html")
    result = run_detection(response, min_confidence=0.99)
    assert result.pattern_name is None
    assert result.needs_review


def test_blocked_response_never_produces_a_confident_match():
    response = make_response(
        "<html><body>Access Denied - please complete a CAPTCHA</body></html>",
        blocked_reason="http_403",
    )
    result = run_detection(response)
    assert result.pattern_name is None
    assert result.needs_review
    assert any("access denied" in w for w in result.warnings)
