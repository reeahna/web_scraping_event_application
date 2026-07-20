"""Identical fixture input + identical configuration must produce identical
candidate ordering, normalized values, fingerprints, validation results, and
selected pattern — excluding runtime-only values (timestamps, run IDs)."""

import dataclasses

from app.extraction.dedup import candidate_fingerprint
from app.extraction.detection import run_detection
from app.extraction.normalize import normalize_candidate
from app.extraction.patterns.jsonld import JsonLdEventPattern
from app.extraction.patterns.static_html import StaticHtmlCardsPattern
from app.extraction.validate import validate_candidate
from app.schemas.extraction import SiteConfiguration
from tests.extraction_helpers import make_response_from_fixture

JSONLD_CONFIG = SiteConfiguration(
    pattern_name="json_ld_event", listing_url="https://example.com/events"
)

HTML_CONFIG = SiteConfiguration(
    pattern_name="generic_html_cards",
    listing_url="https://example.com/events",
    event_container_selector=".event-card",
    field_selectors={
        "title": {"kind": "css", "selector": ".event-title a"},
        "canonical_url": {"kind": "css", "selector": ".event-title a", "attribute": "href"},
        "start_datetime": {"kind": "css", "selector": ".event-date"},
        "venue": {"kind": "css", "selector": ".event-venue"},
    },
    date_formats=["%B %d, %Y"],
)


def _strip_dynamic(candidate):
    """Runtime-only fields (none currently on EventCandidate itself — hash/
    provenance fields are all content-derived, not clock-derived) — kept as
    a no-op hook so the comparison stays explicit about what's excluded."""
    return dataclasses.replace(candidate)


def test_repeated_jsonld_extraction_produces_identical_output():
    response1 = make_response_from_fixture("jsonld_single_event.html")
    response2 = make_response_from_fixture("jsonld_single_event.html")

    candidates1 = [
        normalize_candidate(c, JSONLD_CONFIG)
        for c in JsonLdEventPattern().extract(response1, JSONLD_CONFIG)
    ]
    candidates2 = [
        normalize_candidate(c, JSONLD_CONFIG)
        for c in JsonLdEventPattern().extract(response2, JSONLD_CONFIG)
    ]

    assert [_strip_dynamic(c) for c in candidates1] == [_strip_dynamic(c) for c in candidates2]


def test_repeated_static_html_extraction_produces_identical_ordering_and_values():
    response1 = make_response_from_fixture(
        "static_html_cards.html", final_url="https://example.com/events"
    )
    response2 = make_response_from_fixture(
        "static_html_cards.html", final_url="https://example.com/events"
    )

    raw1 = StaticHtmlCardsPattern().extract(response1, HTML_CONFIG)
    raw2 = StaticHtmlCardsPattern().extract(response2, HTML_CONFIG)
    assert [c.raw["title"] for c in raw1] == [c.raw["title"] for c in raw2]

    normalized1 = [normalize_candidate(c, HTML_CONFIG) for c in raw1]
    normalized2 = [normalize_candidate(c, HTML_CONFIG) for c in raw2]
    assert normalized1 == normalized2


def test_fingerprints_are_stable_across_repeated_runs():
    response = make_response_from_fixture("jsonld_single_event.html")
    candidates = [
        normalize_candidate(c, JSONLD_CONFIG)
        for c in JsonLdEventPattern().extract(response, JSONLD_CONFIG)
    ]
    fp1 = candidate_fingerprint(candidates[0], website_id=1, city_id=None)
    fp2 = candidate_fingerprint(candidates[0], website_id=1, city_id=None)
    assert fp1 == fp2

    # And re-extracting from scratch produces the same fingerprint again.
    response_again = make_response_from_fixture("jsonld_single_event.html")
    candidates_again = [
        normalize_candidate(c, JSONLD_CONFIG)
        for c in JsonLdEventPattern().extract(response_again, JSONLD_CONFIG)
    ]
    fp3 = candidate_fingerprint(candidates_again[0], website_id=1, city_id=None)
    assert fp1 == fp3


def test_validation_results_are_stable_across_repeated_runs():
    response1 = make_response_from_fixture("jsonld_single_event.html")
    response2 = make_response_from_fixture("jsonld_single_event.html")
    c1 = normalize_candidate(
        JsonLdEventPattern().extract(response1, JSONLD_CONFIG)[0], JSONLD_CONFIG
    )
    c2 = normalize_candidate(
        JsonLdEventPattern().extract(response2, JSONLD_CONFIG)[0], JSONLD_CONFIG
    )
    assert validate_candidate(c1, JSONLD_CONFIG) == validate_candidate(c2, JSONLD_CONFIG)


def test_detection_selects_identical_pattern_on_repeated_runs():
    response1 = make_response_from_fixture("wordpress_site_page.html")
    response2 = make_response_from_fixture("wordpress_site_page.html")
    result1 = run_detection(response1)
    result2 = run_detection(response2)
    assert result1.pattern_name == result2.pattern_name == "wordpress_rest"
    assert result1.confidence == result2.confidence


def test_field_source_paths_are_stable_across_repeated_runs():
    response1 = make_response_from_fixture("jsonld_single_event.html")
    response2 = make_response_from_fixture("jsonld_single_event.html")
    c1 = JsonLdEventPattern().extract(response1, JSONLD_CONFIG)[0]
    c2 = JsonLdEventPattern().extract(response2, JSONLD_CONFIG)[0]
    assert c1.field_source_paths == c2.field_source_paths
