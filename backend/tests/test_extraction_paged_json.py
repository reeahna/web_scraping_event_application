"""Paged JSON events pattern: detection, proposal, extraction, pagination.

Fixtures are sanitized bodies shaped like a Spring Data page (`content` plus
page metadata), with record key names deliberately varied between tests — the
pattern must infer them, never assume them, and never branch on a hostname.
"""

from __future__ import annotations

import json

from app.extraction.detection import PagedJsonEventsDetector, run_detection
from app.extraction.inference.policy import DEFAULT_POLICY
from app.extraction.inference.types import ProposalContext
from app.extraction.normalize import normalize_candidate
from app.extraction.pagination import build_pagination_strategy
from app.extraction.patterns.paged_json_events import (
    NAME,
    PagedJsonEventsPattern,
    infer_field_paths,
    local_datetime_string,
)
from app.extraction.registry import REGISTRY
from app.extraction.validate import validate_candidate
from app.schemas.extraction import SiteConfiguration

from .extraction_helpers import make_response

API = "https://events.example.edu/api/events"
TZ = "America/Chicago"


def _page(records, *, number=0, total_pages=3, last=False, pageable=True) -> dict:
    body = {
        "content": records,
        "number": number,
        "size": len(records),
        "totalPages": total_pages,
        "totalElements": total_pages * max(len(records), 1),
        "last": last,
        "first": number == 0,
        "numberOfElements": len(records),
    }
    if pageable:
        body["pageable"] = {"pageNumber": number, "pageSize": len(records)}
    return body


RECORDS = [
    {
        "id": 101,
        "title": "Jazz in the Park",
        "startDate": "2026-10-20T23:00:00Z",
        "endDate": "2026-10-21T01:00:00Z",
        "url": "https://events.example.edu/event/101",
        "location": {"name": "Riverside Park", "address": "1 River Rd"},
        "description": "<p>An evening of jazz.</p>",
        "categories": [{"name": "Music"}, {"name": "Outdoors"}],
        "image": {"url": "https://events.example.edu/img/101.jpg"},
    },
    {
        "id": 102,
        "title": "Lecture: Rivers",
        "startDate": "2026-10-22T17:30:00Z",
        "endDate": "2026-10-22T18:30:00Z",
        "url": "/event/102",
        "location": {"name": "Hall 2"},
        "description": "A talk.",
        "categories": [],
        "image": "javascript:alert(1)",
    },
]


def _response(body: dict, url: str = API):
    return make_response(json.dumps(body), final_url=url, content_type="application/json")


def _config(**json_paths) -> SiteConfiguration:
    return SiteConfiguration(
        pattern_name=NAME, api_endpoint=API, timezone=TZ,
        json_paths={"events_root": "content", **json_paths},
        pagination={"strategy": "page_metadata"},
    )


# --- detection ---------------------------------------------------------------


def test_detects_spring_page_shape():
    result = PagedJsonEventsDetector().detect(_response(_page(RECORDS)))
    assert result.pattern_name == NAME
    assert result.confidence >= 0.6
    assert result.evidence["record_path"] == "content"
    assert result.evidence["field_paths"]["start_datetime"] == "startDate"
    assert result.discovered_endpoints == (API,)


def test_run_detection_selects_paged_json():
    assert run_detection(_response(_page(RECORDS))).pattern_name == NAME


def test_records_without_dates_do_not_match():
    body = _page([{"id": 1, "title": "Footer link", "url": "/a"}] * 3)
    assert PagedJsonEventsDetector().detect(_response(body)).pattern_name is None


def test_generic_data_array_without_page_metadata_does_not_match():
    body = {"data": [{"title": "x", "date": "2026-10-20"}]}
    assert PagedJsonEventsDetector().detect(_response(body)).pattern_name is None


def test_html_does_not_match():
    response = make_response("<html><body>content events</body></html>", final_url=API)
    assert PagedJsonEventsDetector().detect(response).pattern_name is None


def test_registered_with_proposer_and_pagination():
    registration = REGISTRY.get(NAME)
    assert registration.has_proposer
    assert "page_metadata" in registration.supported_pagination
    assert registration.classification == "structured"
    assert not registration.browser_required


# --- field inference ---------------------------------------------------------


def test_infers_varied_key_names_and_nested_values():
    records = [
        {
            "eventId": "a1",
            "name": "Open House",
            "start_date_time": "2026-11-01T10:00:00",
            "permalink": "https://x.example.edu/e/a1",
            "venue": {"venueName": "Union"},
        }
    ]
    paths = infer_field_paths(records)
    assert paths["external_source_id"] == "eventId"
    assert paths["title"] == "name"
    assert paths["start_datetime"] == "start_date_time"
    assert paths["canonical_url"] == "permalink"
    assert paths["venue"] == "venue"


def test_bare_slug_is_not_used_as_a_url():
    paths = infer_field_paths([{"id": 1, "title": "x", "date": "2026-11-01", "url": "open-house"}])
    assert "canonical_url" not in paths


def test_title_is_never_taken_from_a_nested_object():
    paths = infer_field_paths([{"id": 1, "date": "2026-11-01", "location": {"name": "Hall"}}])
    assert "title" not in paths


# --- extraction --------------------------------------------------------------


def _normalized(body: dict, config: SiteConfiguration | None = None):
    config = config or _config()
    candidates = PagedJsonEventsPattern().extract(_response(body), config)
    return [normalize_candidate(c, config, fallback_timezone=TZ) for c in candidates]


def test_extracts_and_normalizes_events_in_local_time():
    events = _normalized(_page(RECORDS))
    assert [e.title for e in events] == ["Jazz in the Park", "Lecture: Rivers"]
    jazz = events[0]
    # 23:00Z on Oct 20 is 18:00 in Chicago (CDT).
    assert jazz.start_date.isoformat() == "2026-10-20"
    assert jazz.start_time.isoformat() == "18:00:00"
    assert jazz.end_time.isoformat() == "20:00:00"
    assert jazz.venue == "Riverside Park"
    assert jazz.source_category == "Music"
    assert jazz.image_url == "https://events.example.edu/img/101.jpg"
    assert jazz.external_source_id == "101"
    assert jazz.description == "An evening of jazz."
    lecture = events[1]
    assert lecture.canonical_url == "https://events.example.edu/event/102"
    assert lecture.image_url is None
    for event in events:
        assert validate_candidate(event, _config()).is_valid


def test_configured_paths_override_inference():
    records = [{"id": 1, "title": "Inferred", "label": "Configured", "date": "2026-11-01"}]
    events = _normalized(_page(records), _config(title="label"))
    assert events[0].title == "Configured"


def test_instance_lists_become_one_event_per_instance():
    records = [
        {
            "id": 7,
            "title": "Film series",
            "instances": [
                {"start": "2026-11-01T19:00:00", "end": "2026-11-01T21:00:00"},
                {"start": "2026-11-08T19:00:00", "end": "2026-11-08T21:00:00"},
            ],
        }
    ]
    events = _normalized(_page(records))
    assert [e.start_date.isoformat() for e in events] == ["2026-11-01", "2026-11-08"]
    assert [e.external_source_id for e in events] == ["7", "7#1"]
    assert all(e.title == "Film series" for e in events)


def test_plain_date_list_is_expanded():
    records = [{"id": 3, "title": "Tours", "dates": ["2026-11-02", "2026-11-09"]}]
    events = _normalized(_page(records))
    assert [e.start_date.isoformat() for e in events] == ["2026-11-02", "2026-11-09"]


def test_machine_timestamps_become_local_wall_clock():
    # 2026-10-20T23:00:00Z in epoch milliseconds and seconds.
    assert local_datetime_string(1792537200000, TZ) == "2026-10-20T18:00:00"
    assert local_datetime_string(1792537200, TZ) == "2026-10-20T18:00:00"
    assert local_datetime_string([2026, 10, 20, 18, 0], TZ) == "2026-10-20T18:00:00"
    assert local_datetime_string([2026, 10, 20], TZ) == "2026-10-20"
    assert local_datetime_string("2026-10-20T18:00:00", TZ) == "2026-10-20T18:00:00"
    assert local_datetime_string("October 20", TZ) == "October 20"


def test_invalid_json_yields_nothing():
    response = make_response("not json", final_url=API, content_type="application/json")
    assert PagedJsonEventsPattern().extract(response, _config()) == []


# --- pagination --------------------------------------------------------------


def _next(body: dict, url: str = API, page_index: int = 0, config=None):
    config = config or _config()
    return build_pagination_strategy(config).next_request(
        _response(body, url), page_index, config,
        visited_urls=frozenset(), seen_body_hashes=frozenset(),
    )


def test_page_metadata_walks_zero_based_pages_keeping_other_params():
    request = _next(_page(RECORDS, number=0), url=f"{API}?size=50")
    assert request is not None
    assert request.url == f"{API}?size=50&page=1"
    request = _next(_page(RECORDS, number=1), url=f"{API}?size=50&page=1", page_index=1)
    assert request.url == f"{API}?size=50&page=2"


def test_page_metadata_stops_on_last_page():
    assert _next(_page(RECORDS, number=2, total_pages=3, last=True)) is None
    assert _next(_page(RECORDS, number=2, total_pages=3, last=False)) is None


def test_page_metadata_stops_on_empty_page():
    assert _next(_page([], number=0, total_pages=5)) is None


def test_page_metadata_handles_one_based_page_numbers():
    body = {"items": [{"title": "x"}], "page": 1, "pageCount": 2}
    config = _config(events_root="items")
    assert _next(body, config=config).url == f"{API}?page=2"
    assert _next({**body, "page": 2}, config=config) is None


def test_page_metadata_respects_max_pages():
    config = SiteConfiguration(
        pattern_name=NAME, api_endpoint=API,
        pagination={"strategy": "page_metadata", "max_pages": 2},
    )
    assert _next(_page(RECORDS, number=1, total_pages=9), page_index=1, config=config) is None


# --- proposal ----------------------------------------------------------------


def _propose(body: dict, url: str = API):
    response = _response(body, url)
    detection = run_detection(response)
    context = ProposalContext(
        response=response, detection=detection, listing_url=url,
        fallback_timezone=TZ, policy=DEFAULT_POLICY,
    )
    return REGISTRY.get(NAME).proposer.propose(context)


def test_proposes_complete_http_configuration():
    proposal = _propose(_page(RECORDS), url=f"{API}?page=0&size=10")
    config = proposal.configuration
    assert config is not None
    assert config.pattern_name == NAME
    assert config.execution_strategy == "http"
    # Observed query dropped; a larger Spring page size requested instead.
    assert config.api_endpoint == f"{API}?size=50"
    assert config.json_paths["events_root"] == "content"
    assert config.json_paths["start_datetime"] == "startDate"
    assert config.pagination.strategy == "page_metadata"
    assert config.timezone == TZ
    assert proposal.missing_required_fields == ()
    assert "canonical_url" in config.required_fields


def test_records_without_links_are_identified_by_id():
    records = [{"id": n, "title": f"E{n}", "startDate": "2026-11-01T10:00:00"} for n in (1, 2)]
    proposal = _propose(_page(records, pageable=False))
    config = proposal.configuration
    assert config.api_endpoint == API
    assert "canonical_url" not in config.required_fields
    assert proposal.missing_required_fields == ()
