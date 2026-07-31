"""Phase 8F part 2: ICS, RSS/Atom, and Algolia extraction patterns.

ICS and RSS/Atom use maintained parsers (icalendar, defusedxml); Algolia reads
a query response and authenticates via a secret reference, never a stored key.
No hostname or site-name branch exists in any of them.
"""

from __future__ import annotations

from app.extraction.detection import run_detection
from app.extraction.inference.service import ConfigurationInferenceService
from app.extraction.normalize import normalize_candidate
from app.extraction.patterns.algolia_search import AlgoliaSearchPattern
from app.extraction.patterns.ics_calendar import IcsCalendarPattern
from app.extraction.patterns.rss_atom_events import RssAtomEventsPattern
from app.extraction.registry import REGISTRY
from app.extraction.validate import validate_candidate
from app.schemas.extraction import SiteConfiguration
from tests.extraction_helpers import make_response_from_fixture

URL = "https://venue.example.org/events"


def _ics(content_type="text/calendar"):
    return make_response_from_fixture("events.ics", final_url=URL, content_type=content_type)


# --- ICS ---------------------------------------------------------------------


def test_ics_is_detected():
    result = run_detection(_ics())
    assert result.pattern_name == "ics_calendar"
    assert result.evidence["all_results"]["ics_calendar"]["vevent_count"] == 4


def test_ics_extraction_maps_vevents():
    config = SiteConfiguration(
        pattern_name="ics_calendar", api_endpoint=URL, required_fields=["title", "start_date"]
    )
    candidates = IcsCalendarPattern().extract(_ics(), config)
    assert len(candidates) == 4

    first = normalize_candidate(candidates[0], config)
    assert first.title == "Opening Night"
    assert first.start_date is not None and first.start_time is not None
    assert first.external_source_id == "evt-001@venue.example.org"
    assert first.venue == "Main Hall"
    assert validate_candidate(first, config).is_valid


def test_ics_all_day_event_has_a_date_but_no_time():
    config = SiteConfiguration(
        pattern_name="ics_calendar", api_endpoint=URL, required_fields=["title", "start_date"]
    )
    candidates = IcsCalendarPattern().extract(_ics(), config)
    all_day = normalize_candidate(candidates[1], config)
    assert all_day.title == "All-Day Community Fair"
    assert all_day.start_date is not None
    assert all_day.start_time is None


def test_ics_cancelled_event_is_flagged_not_dropped():
    config = SiteConfiguration(pattern_name="ics_calendar", api_endpoint=URL)
    candidates = IcsCalendarPattern().extract(_ics(), config)
    cancelled = candidates[2]
    assert "ics_event_cancelled" in cancelled.warnings


def test_ics_recurrence_is_preserved_for_later_expansion():
    config = SiteConfiguration(pattern_name="ics_calendar", api_endpoint=URL)
    candidates = IcsCalendarPattern().extract(_ics(), config)
    recurring = candidates[3]
    assert recurring.raw["recurrence"]["rrule"] is not None
    # One candidate per VEVENT — no guessed expansion at this phase.
    assert len(candidates) == 4


def test_ics_events_without_urls_validate_via_uid_identity():
    from app.extraction.dedup import candidate_fingerprint

    config = SiteConfiguration(
        pattern_name="ics_calendar", api_endpoint=URL, required_fields=["title", "start_date"]
    )
    raw = IcsCalendarPattern().extract(_ics(), config)
    candidates = [normalize_candidate(c, config) for c in raw]
    fair = candidates[1]
    assert fair.canonical_url is None
    # Dedup identity still works via the UID.
    fp = candidate_fingerprint(fair, website_id=1, city_id=1)
    assert fp


def test_ics_proposer_produces_a_config_without_requiring_url():
    proposal = _propose_from(_ics())
    config = proposal.configuration
    assert config is not None
    assert config.pattern_name == "ics_calendar"
    assert "canonical_url" not in config.required_fields
    assert "text/calendar" in config.fetch.allowed_content_types


def test_malformed_ics_does_not_crash():
    from tests.extraction_helpers import make_response

    config = SiteConfiguration(pattern_name="ics_calendar", api_endpoint=URL)
    bad = make_response("not a calendar", final_url=URL)
    assert IcsCalendarPattern().extract(bad, config) == []


# --- RSS / Atom --------------------------------------------------------------


def test_rss_is_detected():
    response = make_response_from_fixture(
        "events_rss.xml", final_url=URL, content_type="application/rss+xml"
    )
    assert run_detection(response).pattern_name == "rss_atom_events"


def test_atom_is_detected():
    response = make_response_from_fixture(
        "events_atom.xml", final_url=URL, content_type="application/atom+xml"
    )
    assert run_detection(response).pattern_name == "rss_atom_events"


def test_rss_extraction_uses_configured_event_date_not_pubdate():
    response = make_response_from_fixture("events_rss.xml", final_url=URL, content_type="text/xml")
    config = SiteConfiguration(
        pattern_name="rss_atom_events",
        listing_url=URL,
        json_paths={"start_datetime": "startdate"},
        required_fields=["title", "start_date"],
    )
    candidates = RssAtomEventsPattern().extract(response, config)
    assert len(candidates) == 3
    first = normalize_candidate(candidates[0], config)
    assert first.title == "Autumn Quartet"
    assert first.start_date.isoformat() == "2026-10-06"  # the event date, not pubDate 2025
    assert first.canonical_url == "https://venue.example.org/events/autumn-quartet"
    assert validate_candidate(first, config).is_valid


def test_atom_extraction_reads_link_href():
    response = make_response_from_fixture("events_atom.xml", final_url=URL, content_type="text/xml")
    config = SiteConfiguration(
        pattern_name="rss_atom_events",
        listing_url=URL,
        json_paths={"start_datetime": "start"},
        required_fields=["title", "start_date"],
    )
    candidates = RssAtomEventsPattern().extract(response, config)
    first = normalize_candidate(candidates[0], config)
    assert first.canonical_url == "https://venue.example.org/events/gallery-talk"


def test_a_feed_without_an_event_date_yields_no_start_and_needs_review():
    response = make_response_from_fixture(
        "rss_no_event_date.xml", final_url=URL, content_type="text/xml"
    )
    proposal = _propose_from(response)
    # A generic feed: the proposer reports the missing date rather than using
    # the publication date.
    assert proposal.configuration is not None
    assert "start_date" in proposal.missing_required_fields


def test_publication_date_is_never_used_as_the_event_date_by_default():
    response = make_response_from_fixture("events_rss.xml", final_url=URL, content_type="text/xml")
    # No start_datetime configured -> start stays absent even though pubDate exists.
    config = SiteConfiguration(pattern_name="rss_atom_events", listing_url=URL)
    candidates = RssAtomEventsPattern().extract(response, config)
    assert all(normalize_candidate(c, config).start_date is None for c in candidates)


# --- Algolia -----------------------------------------------------------------


def test_algolia_query_response_is_detected():
    response = make_response_from_fixture(
        "algolia_response.json", final_url=URL, content_type="application/json"
    )
    assert run_detection(response).pattern_name == "algolia_search"


def test_algolia_extraction_maps_hits():
    response = make_response_from_fixture(
        "algolia_response.json", final_url=URL, content_type="application/json"
    )
    config = SiteConfiguration(
        pattern_name="algolia_search", api_endpoint=URL, required_fields=["title", "start_date"]
    )
    candidates = AlgoliaSearchPattern().extract(response, config)
    assert len(candidates) == 3
    first = normalize_candidate(candidates[0], config)
    assert first.title == "Jazz Evening"
    assert first.external_source_id == "a1"
    assert validate_candidate(first, config).is_valid


def test_algolia_proposer_refuses_without_a_key_reference():
    response = make_response_from_fixture(
        "algolia_response.json", final_url=URL, content_type="application/json"
    )
    proposal = _propose_from(response)
    # No approvable config without a search-only key reference.
    assert proposal.configuration is None
    assert any("key" in (w or "") for w in proposal.warnings) or proposal.error


# --- secret reference: key never stored, resolved at request time ------------


def test_a_raw_key_in_secret_header_refs_is_rejected_by_the_schema():
    import pytest

    with pytest.raises(ValueError):
        SiteConfiguration(
            pattern_name="algolia_search",
            api_endpoint="https://app-dsn.algolia.net/1/indexes/events/query",
            fetch={"secret_header_refs": {"X-Algolia-API-Key": "literal-secret-value"}},
        )


def test_a_secret_reference_is_accepted_and_never_holds_the_value():
    config = SiteConfiguration(
        pattern_name="algolia_search",
        api_endpoint="https://app-dsn.algolia.net/1/indexes/events/query",
        fetch={"secret_header_refs": {"X-Algolia-API-Key": "env:ALGOLIA_SEARCH_KEY"}},
    )
    dumped = config.model_dump(mode="json")
    # The stored config holds the reference, never a secret.
    assert dumped["fetch"]["secret_header_refs"]["X-Algolia-API-Key"] == "env:ALGOLIA_SEARCH_KEY"


def test_the_fetch_layer_resolves_a_secret_reference_into_a_header(monkeypatch):
    import asyncio

    import httpx

    from app.extraction.fetch import HttpFetchStrategy
    from app.extraction.types import FetchRequest

    monkeypatch.setenv("ALGOLIA_SEARCH_KEY", "search-only-abc123")
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json={"hits": [], "nbHits": 0, "nbPages": 0})

    import ipaddress
    from unittest.mock import AsyncMock, patch

    config = SiteConfiguration(
        pattern_name="algolia_search",
        api_endpoint="https://app-dsn.algolia.net/1/indexes/events/query",
        fetch={"secret_header_refs": {"X-Algolia-API-Key": "env:ALGOLIA_SEARCH_KEY"}},
    )
    with patch(
        "app.extraction.fetch.resolve_and_validate_host",
        new=AsyncMock(return_value=[ipaddress.ip_address("93.184.216.34")]),
    ):
        strategy = HttpFetchStrategy(transport=httpx.MockTransport(handler))
        asyncio.run(
            strategy.fetch(
                FetchRequest(url="https://app-dsn.algolia.net/1/indexes/events/query"), config.fetch
            )
        )
    assert seen.get("x-algolia-api-key") == "search-only-abc123"


# --- shared ------------------------------------------------------------------


def _propose_from(response):
    detection = run_detection(response)
    service = ConfigurationInferenceService(REGISTRY)
    context = service.build_context(
        response=response, detection=detection, listing_url=URL, fallback_timezone="UTC"
    )
    return service.propose(context)


def test_all_patterns_are_registered_with_proposers():
    names = set(REGISTRY.names())
    assert {"ics_calendar", "rss_atom_events", "algolia_search", "simpleview_events"} <= names
    assert len(names) == 12
    assert all(REGISTRY.get(n).proposer is not None for n in names)
