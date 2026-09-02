"""Inline JSON-variable pattern: extractor, detector, and proposer.

A JSON event list embedded as a plain JS variable assignment
(`window.eventsListing = [...]`), parsed with json.loads only — never
executed. Modeled on the FullCalendar-driven shape used by seeconstellation.org.
"""

from __future__ import annotations

from app.extraction.detection import InlineJsonEventsDetector, run_detection
from app.extraction.inference.proposers.structured import InlineJsonEventsProposer
from app.extraction.inference.service import ConfigurationInferenceService
from app.extraction.normalize import normalize_candidate
from app.extraction.patterns.inline_json import (
    InlineJsonEventsPattern,
    find_inline_event_variable,
    parse_inline_json_var,
)
from app.extraction.registry import REGISTRY
from app.extraction.validate import validate_candidate
from app.schemas.extraction import SiteConfiguration
from tests.extraction_helpers import make_response

# FullCalendar-style: a date in `start`, a nested ticket URL, a serial timestamp.
_PAGE = """<!DOCTYPE html><html><head><title>Events</title>
<script>
  var config = {theme: "dark"};
  window.eventsListing = [
    {"serial":1788463800,"title":"Heist","start":"2026-09-03",
     "extendedProps":{"buyUrl":"https://x.org/e/110201","time":"7:30 PM"}},
    {"serial":1788550200,"title":"Cabaret","start":"2026-09-04",
     "extendedProps":{"buyUrl":"https://x.org/e/110202","time":"8:00 PM"}},
    {"serial":1788636600,"title":"Recital","start":"2026-09-05",
     "extendedProps":{"buyUrl":"https://x.org/e/110203","time":"6:00 PM"}}
  ];
</script></head><body><div id="calendar"></div></body></html>"""

# A page whose only JS array is not event-like (no title/date) must not match.
_NON_EVENT_PAGE = """<html><head><script>
  window.menu = [{"id":1,"label":"Home"},{"id":2,"label":"About"},{"id":3,"label":"Shop"}];
</script></head><body></body></html>"""

_CONFIG = SiteConfiguration(
    pattern_name="inline_json_events",
    listing_url="https://x.org/events",
    timezone="America/Indiana/Indianapolis",
    json_paths={
        "events_root": "eventsListing",
        "title": "title",
        "start_datetime": "start",
        "canonical_url": "extendedProps.buyUrl",
    },
)


def test_parse_inline_json_var_reads_the_literal():
    value = parse_inline_json_var(_PAGE, "eventsListing")
    assert isinstance(value, list) and len(value) == 3
    assert value[0]["title"] == "Heist"
    # window. prefix is optional in the lookup.
    assert parse_inline_json_var(_PAGE, "window.eventsListing") == value


def test_extractor_reads_nested_url_path():
    candidates = InlineJsonEventsPattern().extract(make_response(_PAGE), _CONFIG)
    assert {c.raw["title"] for c in candidates} == {"Heist", "Cabaret", "Recital"}
    heist = next(c for c in candidates if c.raw["title"] == "Heist")
    assert heist.raw["canonical_url"] == "https://x.org/e/110201"


def test_extracted_events_normalize_and_validate():
    candidates = InlineJsonEventsPattern().extract(make_response(_PAGE), _CONFIG)
    valid = [
        c for c in candidates
        if validate_candidate(
            normalize_candidate(c, _CONFIG, fallback_timezone="America/Indiana/Indianapolis"),
            _CONFIG,
        ).is_valid
    ]
    assert len(valid) == 3


def test_detector_finds_the_event_variable():
    result = InlineJsonEventsDetector().detect(make_response(_PAGE))
    assert result.pattern_name == "inline_json_events"
    assert result.evidence["events_root"] == "eventsListing"
    assert result.evidence["array_size"] == 3


def test_detector_ignores_non_event_arrays():
    result = InlineJsonEventsDetector().detect(make_response(_NON_EVENT_PAGE))
    assert result.pattern_name is None
    assert find_inline_event_variable(_NON_EVENT_PAGE) is None


def test_proposer_configures_events_root_and_flags_nested_url():
    response = make_response(_PAGE)
    detection = run_detection(response)
    assert detection.pattern_name == "inline_json_events"
    context = ConfigurationInferenceService(REGISTRY).build_context(
        response=response, detection=detection,
        listing_url="https://x.org/events", fallback_timezone="America/Indiana/Indianapolis",
    )
    proposal = InlineJsonEventsProposer().propose(context)
    config = proposal.configuration
    assert config.json_paths["events_root"] == "eventsListing"
    assert config.json_paths["title"] == "title"
    # The URL is nested under extendedProps, which top-level inference can't
    # place — so it is honestly reported as missing rather than mis-defaulted.
    assert "canonical_url" in proposal.missing_required_fields
