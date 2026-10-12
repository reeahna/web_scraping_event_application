"""Campus Labs Engage pattern: detection (API response and page), extraction,
and a proposal whose request recipe asks for upcoming events page by page.
Synthetic fixtures; no hostname branching."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from app.extraction.detection import CampusLabsEngageDetector, run_detection
from app.extraction.inference.policy import DEFAULT_POLICY
from app.extraction.inference.types import ProposalContext
from app.extraction.normalize import normalize_candidate
from app.extraction.patterns.campus_labs_engage import (
    NAME,
    CampusLabsEngagePattern,
    engage_base,
    search_endpoint,
)
from app.extraction.registry import REGISTRY
from app.extraction.request_recipe import render_recipe
from app.extraction.structured_candidates import analyze_response
from app.extraction.validate import validate_candidate
from app.schemas.extraction import SiteConfiguration

from .extraction_helpers import make_response

HOSTED = "https://school.campuslabs.example/engage/events"
HOSTED_API = "https://school.campuslabs.example/engage/api/discovery/event/search"
CUSTOM = "https://involved.example.edu/events"
CUSTOM_API = "https://involved.example.edu/api/discovery/event/search"
TZ = "America/Indiana/Indianapolis"


def _record(n: int) -> dict:
    return {
        "id": str(1000 + n),
        "institutionId": 7,
        "organizationId": 55,
        "organizationName": "Chess Club",
        "name": f"Club Night {n}",
        "description": "<p>Bring a board.</p>",
        "location": "Union Room 2",
        "startsOn": "2026-10-20T23:00:00+00:00",
        "endsOn": "2026-10-21T01:00:00+00:00",
        "imagePath": "abc123.png",
        "categoryNames": ["Social", "Games"],
        "status": "Approved",
    }


def _api_response(url: str = f"{HOSTED_API}?take=15&skip=0", records=None):
    if records is None:
        records = [_record(1), _record(2)]
    body = {"@odata.count": 2, "value": records}
    return make_response(json.dumps(body), final_url=url, content_type="application/json")


def test_endpoint_follows_the_platform_route_from_either_address():
    assert search_endpoint(HOSTED) == HOSTED_API
    assert search_endpoint(CUSTOM) == CUSTOM_API
    assert engage_base(HOSTED_API) == "https://school.campuslabs.example/engage"


def test_detects_the_discovery_api_response():
    result = CampusLabsEngageDetector().detect(_api_response())
    assert result.pattern_name == NAME
    assert result.discovered_endpoints == (HOSTED_API,)
    assert run_detection(_api_response()).pattern_name == NAME


def test_detects_an_engage_page_by_its_platform_markers():
    html = (
        '<html><head><script src="https://static.campuslabsengage.com/discovery/app.js">'
        "</script></head><body><div id='discovery-bottom'></div>"
        "<script>window.initialAppState = {};</script></body></html>"
    )
    result = CampusLabsEngageDetector().detect(make_response(html, final_url=CUSTOM))
    assert result.pattern_name == NAME
    assert result.discovered_endpoints == (CUSTOM_API,)


def test_other_odata_json_is_not_engage():
    body = json.dumps({"value": [{"id": 1, "name": "x", "startsOn": "2026-01-01"}]})
    response = make_response(body, final_url=CUSTOM_API, content_type="application/json")
    assert CampusLabsEngageDetector().detect(response).pattern_name is None


def test_ordinary_page_is_not_engage():
    response = make_response("<html><body>events</body></html>", final_url=CUSTOM)
    assert CampusLabsEngageDetector().detect(response).pattern_name is None


def test_observed_api_is_a_structured_candidate():
    payload = {"@odata.count": 2, "value": [_record(1), _record(2)]}
    analysis = analyze_response(url=HOSTED_API, payload=payload, listing_url=HOSTED)
    assert analysis.is_event_candidate


def test_extracts_and_normalizes_events():
    config = SiteConfiguration(pattern_name=NAME, api_endpoint=HOSTED_API, timezone=TZ)
    candidates = CampusLabsEngagePattern().extract(_api_response(), config)
    events = [normalize_candidate(c, config, fallback_timezone=TZ) for c in candidates]
    assert [e.title for e in events] == ["Club Night 1", "Club Night 2"]
    event = events[0]
    assert event.canonical_url == "https://school.campuslabs.example/engage/event/1001"
    assert event.start_date.isoformat() == "2026-10-20"
    assert event.start_time.isoformat() == "19:00:00"  # 23:00Z in Indianapolis (EDT)
    assert event.venue == "Union Room 2"
    assert event.description == "Bring a board."
    assert event.source_category == "Social"
    assert event.external_source_id == "1001"
    assert event.image_url.startswith("https://se-images.campuslabs.com/clink/images/abc123.png")
    assert validate_candidate(event, config).is_valid


def test_custom_domain_event_links_have_no_engage_prefix():
    config = SiteConfiguration(pattern_name=NAME, api_endpoint=CUSTOM_API, timezone=TZ)
    candidates = CampusLabsEngagePattern().extract(_api_response(url=CUSTOM_API), config)
    assert candidates[0].raw["canonical_url"] == "https://involved.example.edu/event/1001"


def test_proposal_requests_upcoming_events_page_by_page():
    response = _api_response()
    detection = run_detection(response)
    context = ProposalContext(
        response=response, detection=detection, listing_url=response.final_url,
        fallback_timezone=TZ, policy=DEFAULT_POLICY,
    )
    proposal = REGISTRY.get(NAME).proposer.propose(context)
    config = proposal.configuration
    assert config is not None and config.execution_strategy == "http"
    recipe = config.request_recipe
    assert recipe.endpoint == HOSTED_API
    now = datetime(2026, 10, 12, 19, 0, tzinfo=UTC)
    page2 = render_recipe(recipe, now_utc=now, timezone=TZ, page_offset=50, page_number=2)
    assert page2.params["skip"] == "50"
    assert page2.params["take"] == "50"
    # Start of today in Indianapolis (EDT, UTC-4), as a plain value — not quoted JSON.
    assert page2.params["endsAfter"] == "2026-10-12T04:00:00.000Z"
    assert page2.params["status"] == "Approved"
    assert REGISTRY.get(NAME).classification == "structured"
