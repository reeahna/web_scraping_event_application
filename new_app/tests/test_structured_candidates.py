"""Deterministic structured-source classification and scoring
(app.extraction.structured_candidates)."""

import pytest

from app.extraction.structured_candidates import (
    EVENT_CANDIDATE_THRESHOLD,
    FIRST_PARTY_EVENT_CANDIDATE,
    FIRST_PARTY_OTHER,
    THIRD_PARTY_FUNCTIONAL,
    THIRD_PARTY_TELEMETRY,
    analyze_response,
    is_telemetry,
    registrable_domain,
    same_registrable_domain,
    sanitize_url,
)

LISTING = "https://www.visitbloomington.com/events/events-this-weekend/"

_EVENT_BODY = {
    "docs": {
        "count": 42,
        "docs": [
            {"title": "Autumn Quartet", "startDate": "2026-10-06",
             "url": "/event/autumn/123/", "id": "123", "location": "Buskirk"},
            {"title": "Winter Chorus", "startDate": "2026-11-13",
             "url": "/event/winter/124/", "id": "124", "location": "Ivy Tech"},
            {"title": "Spring Recital", "startDate": "2027-03-07",
             "url": "/event/spring/125/", "id": "125", "location": "Buskirk"},
        ],
    }
}


def _analyze(url, payload, listing=LISTING):
    return analyze_response(url=url, payload=payload, listing_url=listing)


# --- registrable domain ------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.visitbloomington.com/x", "visitbloomington.com"),
        ("https://visitbloomington.com", "visitbloomington.com"),
        ("https://api.sub.example.co.uk/v1", "example.co.uk"),
        ("https://events.example.com.au/list", "example.com.au"),
        ("http://127.0.0.1:8080/x", "127.0.0.1"),
    ],
)
def test_registrable_domain(url, expected):
    assert registrable_domain(url) == expected


def test_same_registrable_domain_across_subdomains():
    assert same_registrable_domain(
        "https://www.visitbloomington.com/a", "https://api.visitbloomington.com/b"
    )
    assert not same_registrable_domain(
        "https://www.visitbloomington.com/a", "https://spotify.com/b"
    )


# --- classification ----------------------------------------------------------


def test_first_party_event_json_is_a_candidate():
    a = _analyze(
        "https://www.visitbloomington.com/includes/rest_v2/plugins_events_events_by_date/find/",
        _EVENT_BODY,
    )
    assert a.classification == FIRST_PARTY_EVENT_CANDIDATE
    assert a.is_event_candidate
    assert a.event_likeness_score >= EVENT_CANDIDATE_THRESHOLD
    assert a.record_array_path == "docs.docs"
    assert "title" in a.sample_field_names


def test_same_domain_unrelated_json_is_down_ranked():
    a = _analyze(
        "https://www.visitbloomington.com/api/site-config",
        {"theme": "light", "menu": ["home", "about"]},
    )
    assert a.classification == FIRST_PARTY_OTHER
    assert not a.is_event_candidate


def test_aggregate_only_endpoint_ranks_below_event_records():
    event = _analyze(
        "https://www.visitbloomington.com/includes/rest_v2/plugins_events_events_by_date/find/",
        _EVENT_BODY,
    )
    aggregate = _analyze(
        "https://www.visitbloomington.com/includes/rest_v2/plugins_events_events/aggregate/",
        {"aggregations": {"categories": {"Music": 10, "Arts": 4}}, "total": 42},
    )
    assert aggregate.event_likeness_score < event.event_likeness_score
    assert not aggregate.is_event_candidate


@pytest.mark.parametrize(
    "url",
    [
        "https://pixel.spotify.com/v1/track",
        "https://ct.pinterest.com/v3/",
        "https://maps.googleapis.com/maps/api/js",
        "https://www.google-analytics.com/g/collect?v=2&tid=X",
        "https://www.googletagmanager.com/gtm.js",
    ],
)
def test_third_party_telemetry_is_ignored(url):
    a = _analyze(url, {"anything": 1})
    assert a.classification == THIRD_PARTY_TELEMETRY
    assert not a.is_event_candidate


def test_cross_origin_non_telemetry_is_third_party_functional():
    a = _analyze("https://cdn.othervendor.com/widget/data.json", _EVENT_BODY)
    assert a.classification == THIRD_PARTY_FUNCTIONAL
    assert not a.is_event_candidate


def test_is_telemetry_direct():
    assert is_telemetry("https://www.google-analytics.com/g/collect")
    assert is_telemetry("https://analytics.example.com/collect")
    assert not is_telemetry("https://www.visitbloomington.com/includes/rest_v2/events/find/")


# --- scoring -----------------------------------------------------------------


def test_event_json_outranks_empty_and_telemetry():
    event = _analyze("https://www.visitbloomington.com/events/find/", _EVENT_BODY)
    empty = _analyze("https://www.visitbloomington.com/events/find/", {})
    assert event.event_likeness_score > empty.event_likeness_score
    assert empty.event_likeness_score < EVENT_CANDIDATE_THRESHOLD


def test_ids_and_dates_increase_score():
    rich = _analyze("https://www.visitbloomington.com/events/find/", _EVENT_BODY)
    bare = _analyze(
        "https://www.visitbloomington.com/events/find/",
        {"docs": {"docs": [{"headline": "A"}, {"headline": "B"}, {"headline": "C"}]}},
    )
    assert rich.event_likeness_score > bare.event_likeness_score


def test_multiple_records_score_higher_than_single():
    single = _analyze(
        "https://www.visitbloomington.com/events/find/",
        {"docs": {"docs": [_EVENT_BODY["docs"]["docs"][0]]}},
    )
    many = _analyze("https://www.visitbloomington.com/events/find/", _EVENT_BODY)
    assert many.event_likeness_score > single.event_likeness_score


def test_cross_origin_penalised_versus_same_domain():
    same = _analyze("https://www.visitbloomington.com/events/find/", _EVENT_BODY)
    cross = analyze_response(
        url="https://events.thirdparty.io/find/", payload=_EVENT_BODY, listing_url=LISTING
    )
    # Cross-origin is classified out entirely (not an event candidate) even
    # though the body is identical.
    assert same.is_event_candidate
    assert not cross.is_event_candidate


# --- inspection --------------------------------------------------------------


def test_top_level_list_inspection():
    a = _analyze(
        "https://www.visitbloomington.com/events/find/",
        [{"title": "A", "startDate": "2026-01-01", "id": 1}],
    )
    assert a.top_level_type == "list"
    assert a.record_array_path == ""


def test_nested_record_array_discovery():
    a = _analyze(
        "https://www.visitbloomington.com/events/find/",
        {"response": {"results": [{"name": "A", "date": "2026-01-01", "id": 1}]}},
    )
    assert a.top_level_type == "object"
    assert a.record_array_path == "response.results"


def test_oversized_response_rejected(monkeypatch):
    import app.extraction.structured_candidates as sc

    monkeypatch.setattr(sc, "_MAX_INSPECT_BYTES", 10)
    a = _analyze("https://www.visitbloomington.com/events/find/", _EVENT_BODY)
    assert a.reject_reason == "response exceeds the inspection size bound"
    assert not a.is_event_candidate


def test_scalar_payload_handled():
    a = _analyze("https://www.visitbloomington.com/events/find/", "just a string")
    assert a.top_level_type == "scalar"
    assert not a.is_event_candidate


def test_evidence_stores_no_body():
    a = _analyze("https://www.visitbloomington.com/events/find/", _EVENT_BODY)
    evidence = a.to_evidence()
    # Only bounded metadata — never the payload itself.
    assert "body" not in evidence
    assert "payload" not in evidence
    assert set(evidence) >= {"url", "classification", "event_likeness_score", "record_array_path"}


# --- URL redaction -----------------------------------------------------------


def test_sanitize_url_strips_query_and_fragment():
    dirty = "https://www.visitbloomington.com/1/indexes/events/query?x-algolia-key=SECRET#frag"
    assert sanitize_url(dirty) == "https://www.visitbloomington.com/1/indexes/events/query"


def test_analysis_url_is_sanitized():
    a = _analyze(
        "https://www.visitbloomington.com/events/find/?token=abc123&session=xyz", _EVENT_BODY
    )
    assert "token" not in a.sanitized_url
    assert "abc123" not in a.sanitized_url
