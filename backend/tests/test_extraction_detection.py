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


def test_jsonld_detector_matches_itemlist_of_events():
    html = (
        '<html><head><script type="application/ld+json">'
        '{"@context":"https://schema.org","@type":"ItemList","itemListElement":['
        '{"@type":"ListItem","position":1,"item":{"@type":"Event","name":"A",'
        '"startDate":"2026-09-01","url":"https://example.com/e/a"}},'
        '{"@type":"ListItem","position":2,"item":{"@type":"Event","name":"B",'
        '"startDate":"2026-09-02","url":"https://example.com/e/b"}}'
        ']}</script></head><body></body></html>'
    )
    result = JsonLdDetector().detect(make_response(html))
    assert result.pattern_name == "json_ld_event"
    assert result.evidence["event_blocks_found"] == 2


def test_jsonld_detector_ignores_non_event_itemlist():
    html = (
        '<html><head><script type="application/ld+json">'
        '{"@context":"https://schema.org","@type":"ItemList","itemListElement":['
        '{"@type":"ListItem","position":1,"item":{"@type":"WebPage","name":"Home",'
        '"url":"https://example.com/"}}]}</script></head><body></body></html>'
    )
    result = JsonLdDetector().detect(make_response(html))
    assert result.pattern_name is None


def test_date_like_regex_matches_day_first_dates():
    from app.extraction.detection import DATE_LIKE_RE

    # Day-first ("03 September"), the reverse of the leading "Month DD" form.
    assert DATE_LIKE_RE.search("03 September Thursday")
    assert DATE_LIKE_RE.search("24 Jul")
    # The month-first form still matches.
    assert DATE_LIKE_RE.search("September 3, 2026")


def test_wordpress_rest_does_not_outrank_a_real_event_listing():
    # A WordPress site whose events are an actual card listing: generic_html_cards
    # (the real events) must win over wordpress_rest, whose wp/v2/posts would grab
    # blog posts. Generic "this is WordPress" evidence alone must not outrank it.
    cards = "".join(
        f'<div class="col event-card"><a href="/event/{i}">Show {i}</a>'
        f'<span>0{i} September</span></div>'
        for i in range(1, 6)
    )
    html = (
        '<html><head><meta name="generator" content="WordPress 6.5">'
        '<link rel="https://api.w.org/" href="https://x.org/wp-json/">'
        f"</head><body><div class=\"list\">{cards}</div></body></html>"
    )
    result = run_detection(make_response(html, final_url="https://x.org/events/"))
    assert result.pattern_name == "generic_html_cards"
