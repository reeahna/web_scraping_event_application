"""Phase 8F part 1: the JSON-in-script extraction patterns.

embedded_json, next_data, and nuxt_payload all parse strict JSON already in
the page (never executing anything) and read the event list from a
config-provided or discovered `events_root`. No hostname or site-name branch
exists in any of them.
"""

from __future__ import annotations

from app.extraction.detection import run_detection
from app.extraction.inference.json_events import find_event_arrays, infer_field_paths
from app.extraction.inference.service import ConfigurationInferenceService
from app.extraction.normalize import normalize_candidate
from app.extraction.patterns.embedded_json import EmbeddedJsonPattern
from app.extraction.patterns.next_data import NextDataPattern
from app.extraction.patterns.nuxt_payload import NuxtPayloadPattern
from app.extraction.registry import REGISTRY
from app.extraction.validate import validate_candidate
from app.schemas.extraction import SiteConfiguration
from tests.extraction_helpers import make_response_from_fixture

URL = "https://venue.example.org/events"


# --- event-array finder ------------------------------------------------------


def test_the_finder_picks_the_event_array_not_the_nav_array():
    document = {
        "nav": [
            {"label": "Home", "href": "/"},
            {"label": "A", "href": "/a"},
            {"label": "B", "href": "/b"},
        ],
        "events": [
            {"title": "A", "start": "2026-10-06"},
            {"title": "B", "start": "2026-10-07"},
            {"title": "C", "start": "2026-10-08"},
        ],
    }
    candidates = find_event_arrays(document)
    assert candidates
    assert candidates[0].path == "events"


def test_the_finder_reports_nothing_when_no_array_looks_like_events():
    document = {"nav": [{"label": "Home"}, {"label": "About"}, {"label": "Contact"}]}
    assert find_event_arrays(document) == []


def test_field_paths_map_to_real_keys():
    sample = {"name": "X", "startDate": "2026-10-06", "url": "/x", "id": 7}
    paths = infer_field_paths(sample)
    assert paths["title"] == "name"
    assert paths["start_datetime"] == "startDate"
    assert paths["canonical_url"] == "url"
    assert paths["external_source_id"] == "id"


# --- detection ---------------------------------------------------------------


def test_embedded_json_is_detected():
    response = make_response_from_fixture("embedded_json_events.html", final_url=URL)
    result = run_detection(response)
    assert result.pattern_name == "embedded_json"
    assert result.evidence["all_results"]["embedded_json"]["events_root"] == "events"


def test_next_data_is_detected():
    response = make_response_from_fixture("next_data_events.html", final_url=URL)
    result = run_detection(response)
    assert result.pattern_name == "next_data"
    assert "pageProps" in result.evidence["all_results"]["next_data"]["events_root"]


def test_nuxt_payload_is_detected():
    response = make_response_from_fixture("nuxt_payload_events.html", final_url=URL)
    result = run_detection(response)
    assert result.pattern_name == "nuxt_payload"


def test_a_nuxt_page_with_only_a_js_assignment_is_browser_required_not_parsed():
    response = make_response_from_fixture("nuxt_browser_required.html", final_url=URL)
    result = run_detection(response)
    # No parseable payload -> not matched here, and flagged for the browser path.
    assert result.pattern_name != "nuxt_payload"
    assert result.browser_required is True


# --- extraction --------------------------------------------------------------


def _config(pattern: str, events_root: str) -> SiteConfiguration:
    return SiteConfiguration(
        pattern_name=pattern, listing_url=URL, json_paths={"events_root": events_root}
    )


def test_embedded_json_extraction_produces_typed_events():
    response = make_response_from_fixture("embedded_json_events.html", final_url=URL)
    config = _config("embedded_json", "events")
    candidates = EmbeddedJsonPattern().extract(response, config)
    assert len(candidates) == 4
    normalized = normalize_candidate(candidates[0], config)
    assert normalized.title == "Opening Night"
    assert normalized.start_date is not None
    assert normalized.canonical_url == "https://venue.example.org/events/opening-night"
    assert validate_candidate(normalized, config).is_valid


def test_next_data_extraction_reads_page_props():
    response = make_response_from_fixture("next_data_events.html", final_url=URL)
    config = SiteConfiguration(
        pattern_name="next_data",
        listing_url=URL,
        json_paths={
            "events_root": "props.pageProps.events",
            "title": "name",
            "canonical_url": "url",
            "start_datetime": "startDate",
        },
    )
    candidates = NextDataPattern().extract(response, config)
    assert len(candidates) == 4
    normalized = normalize_candidate(candidates[0], config)
    assert normalized.title == "Autumn Quartet"
    assert normalized.start_date is not None


def test_nuxt_payload_extraction():
    response = make_response_from_fixture("nuxt_payload_events.html", final_url=URL)
    config = _config("nuxt_payload", "data.events")
    candidates = NuxtPayloadPattern().extract(response, config)
    assert len(candidates) == 4
    assert normalize_candidate(candidates[0], config).title == "Harvest Market"


def test_embedded_json_without_a_configured_root_extracts_nothing():
    """No guessing: without events_root and no top-level list, nothing is
    pulled out."""
    response = make_response_from_fixture("embedded_json_events.html", final_url=URL)
    config = SiteConfiguration(pattern_name="embedded_json", listing_url=URL)
    assert EmbeddedJsonPattern().extract(response, config) == []


def test_invalid_json_does_not_crash():
    from tests.extraction_helpers import make_response

    response = make_response("<script type='application/json'>{ not json </script>", final_url=URL)
    config = _config("embedded_json", "events")
    assert EmbeddedJsonPattern().extract(response, config) == []


# --- proposers ---------------------------------------------------------------


def _propose(fixture: str, url: str = URL):
    response = make_response_from_fixture(fixture, final_url=url)
    detection = run_detection(response)
    service = ConfigurationInferenceService(REGISTRY)
    context = service.build_context(
        response=response, detection=detection, listing_url=url, fallback_timezone="UTC"
    )
    return service.propose(context)


def test_embedded_json_proposer_discovers_root_and_maps_fields():
    proposal = _propose("embedded_json_events.html")
    config = proposal.configuration
    assert config is not None
    assert config.pattern_name == "embedded_json"
    assert config.json_paths["events_root"] == "events"
    assert config.json_paths["title"] == "title"
    assert proposal.missing_required_fields == ()


def test_next_data_proposer_discovers_the_page_props_path():
    proposal = _propose("next_data_events.html")
    assert proposal.configuration is not None
    assert "pageProps" in proposal.configuration.json_paths["events_root"]


def test_nuxt_proposer_discovers_the_data_events_path():
    proposal = _propose("nuxt_payload_events.html")
    assert proposal.configuration is not None
    assert proposal.configuration.json_paths["events_root"] == "data.events"


# --- provider independence ---------------------------------------------------


def test_no_hostname_branching_in_the_new_patterns():
    import ast
    from pathlib import Path

    for module in (
        "app/extraction/patterns/embedded_json.py",
        "app/extraction/patterns/next_data.py",
        "app/extraction/patterns/nuxt_payload.py",
        "app/extraction/inference/json_events.py",
        "app/extraction/inference/proposers/json_scripts.py",
    ):
        tree = ast.parse(Path(module).read_text(encoding="utf-8"))
        docstrings = {
            id(n.body[0].value)
            for n in ast.walk(tree)
            if isinstance(n, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
            and n.body
            and isinstance(n.body[0], ast.Expr)
            and isinstance(n.body[0].value, ast.Constant)
        }
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
            ):
                lowered = node.value.lower()
                for token in ("iuauditorium", "buskirk", "chumley", "://"):
                    assert token not in lowered, f"{module}: {node.value!r}"


# --- nested field inference (FullCalendar-style extendedProps) ---------------


def test_infer_field_paths_finds_a_nested_url():
    # No top-level URL; the event's link is nested under extendedProps.
    record = {
        "title": "Heist",
        "start": "2026-09-03",
        "extendedProps": {"buyUrl": "https://x.org/e/110201", "time": "7:30 PM"},
    }
    paths = infer_field_paths(record)
    assert paths["title"] == "title"
    assert paths["start_datetime"] == "start"
    assert paths["canonical_url"] == "extendedProps.buyUrl"


def test_infer_field_paths_prefers_a_top_level_url_over_a_nested_one():
    record = {
        "title": "A",
        "start": "2026-09-03",
        "url": "https://x.org/real",
        "extendedProps": {"buyUrl": "https://x.org/buy"},
    }
    assert infer_field_paths(record)["canonical_url"] == "url"
