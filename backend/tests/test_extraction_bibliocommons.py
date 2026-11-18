from app.extraction.detection import BiblioCommonsEventsDetector, run_detection
from app.extraction.inference.policy import AutoOnboardingPolicy
from app.extraction.inference.types import ProposalContext
from app.extraction.normalize import normalize_candidate
from app.extraction.pagination import build_pagination_strategy
from app.extraction.patterns.bibliocommons_events import (
    BiblioCommonsEventsPattern,
    library_from_url,
)
from app.extraction.registry import REGISTRY
from app.schemas.extraction import SiteConfiguration
from tests.extraction_helpers import make_response, make_response_from_fixture

ENDPOINT = (
    "https://gateway.bibliocommons.com/v2/libraries/examplelib/events?locale=en-US&limit=100"
)
CONFIG = SiteConfiguration(
    pattern_name="bibliocommons_events",
    api_endpoint=f"{ENDPOINT}&page=1",
    timezone="America/Chicago",
    pagination={
        "strategy": "path_page",
        "page_path_template": f"{ENDPOINT}&page={{page}}",
        "max_pages": 5,
        "max_events": 500,
    },
)


def _api_response():
    return make_response_from_fixture(
        "bibliocommons_events.json",
        final_url=f"{ENDPOINT}&page=1",
        content_type="application/json",
    )


def test_library_from_url():
    assert library_from_url(ENDPOINT) == "examplelib"
    assert library_from_url("https://examplelib.bibliocommons.com/events") == "examplelib"
    assert library_from_url("https://gateway.bibliocommons.com/") is None
    assert library_from_url("https://example.com/events") is None


def test_extract_looks_up_records_venues_types_and_images():
    candidates = BiblioCommonsEventsPattern().extract(_api_response(), CONFIG)
    # The cancelled event and the dangling id are skipped.
    assert [c.raw["title"] for c in candidates] == ["Knitting Circle", "Story Time in the Park"]

    first, second = candidates
    assert first.raw["external_source_id"] == "ev1"
    assert first.raw["start_datetime"] == "2026-12-15T18:30"
    assert first.raw["venue"] == "Main Library"
    assert first.raw["address"] == "100 Main St, Exampleton, IL 60000"
    assert first.raw["source_category"] == "Crafts & DIY"
    assert first.raw["image"] == "https://examplelib.bibliocommons.com/l.png"
    assert first.raw["canonical_url"] == "https://examplelib.bibliocommons.com/events/ev1"

    assert second.raw["venue"] == "Offsite: Community Center"
    assert second.raw["latitude"] == 42.02
    assert second.raw["image"] is None
    assert second.raw["source_category"] is None


def test_candidates_normalize_to_local_times():
    candidate = BiblioCommonsEventsPattern().extract(_api_response(), CONFIG)[0]
    event = normalize_candidate(candidate, CONFIG)
    assert str(event.start_date) == "2026-12-15"
    assert str(event.start_time).startswith("18:30")
    assert event.venue == "Main Library"


def test_extract_ignores_other_json():
    response = make_response('{"events": [1, 2]}', final_url=ENDPOINT)
    assert BiblioCommonsEventsPattern().extract(response, CONFIG) == []


def test_detector_matches_api_response_and_wins_detection():
    result = BiblioCommonsEventsDetector().detect(_api_response())
    assert result.pattern_name == "bibliocommons_events"
    assert result.confidence >= 0.9
    assert result.discovered_endpoints == (ENDPOINT,)
    assert run_detection(_api_response()).pattern_name == "bibliocommons_events"


def test_detector_matches_library_page_and_not_other_pages():
    page = make_response(
        "<html><script src='https://cdn.bibliocommons.com/app.js'></script></html>",
        final_url="https://examplelib.bibliocommons.com/events",
    )
    result = BiblioCommonsEventsDetector().detect(page)
    assert result.pattern_name == "bibliocommons_events"
    assert result.discovered_endpoints == (ENDPOINT,)

    other = make_response("<html>bibliocommons</html>", final_url="https://example.com/events")
    assert BiblioCommonsEventsDetector().detect(other).pattern_name is None


def test_proposer_builds_paged_configuration():
    response = _api_response()
    detection = BiblioCommonsEventsDetector().detect(response)
    proposal = REGISTRY.get("bibliocommons_events").proposer.propose(
        ProposalContext(
            response=response,
            detection=detection,
            listing_url="https://examplelib.bibliocommons.com/events",
            fallback_timezone="America/Chicago",
            policy=AutoOnboardingPolicy(),
        )
    )
    config = proposal.configuration
    assert config.pattern_name == "bibliocommons_events"
    assert config.api_endpoint == f"{ENDPOINT}&page=1"
    assert config.pagination.strategy == "path_page"
    assert config.pagination.page_path_template == f"{ENDPOINT}&page={{page}}"


def test_path_page_pagination_walks_pages():
    strategy = build_pagination_strategy(CONFIG)
    request = strategy.next_request(
        _api_response(), 0, CONFIG, visited_urls=frozenset(), seen_body_hashes=frozenset()
    )
    assert request.url == f"{ENDPOINT}&page=2"
