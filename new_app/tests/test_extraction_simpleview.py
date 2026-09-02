"""Simpleview events pattern: detection, proposal, extraction, security.

The pattern is generic and configuration-driven — these tests use sanitized
fixtures modeled on the confirmed Simpleview response shape (records at
`docs.docs`), never a real captured body, and assert no hostname branching.
"""

from __future__ import annotations

import json

import pydantic
import pytest

from app.extraction.dedup import dedupe_within_run
from app.extraction.detection import SimpleviewEventsDetector, run_detection
from app.extraction.inference.policy import DEFAULT_POLICY
from app.extraction.inference.types import ProposalContext
from app.extraction.normalize import normalize_candidate
from app.extraction.patterns.simpleview_events import NAME, SimpleviewEventsPattern
from app.extraction.registry import REGISTRY
from app.extraction.validate import validate_candidate
from app.schemas.extraction import SiteConfiguration

from .extraction_helpers import make_response, make_response_from_fixture

FIND_URL = "https://www.visitbloomington.com/includes/rest_v2/plugins_events_events_by_date/find/"
TZ = "America/Indiana/Indianapolis"
CONFIG = SiteConfiguration(pattern_name=NAME, api_endpoint=FIND_URL, timezone=TZ)


def _page1():
    return make_response_from_fixture(
        "simpleview_events_page1.json", final_url=FIND_URL, content_type="application/json"
    )


def _normalized():
    candidates = SimpleviewEventsPattern().extract(_page1(), CONFIG)
    return [normalize_candidate(c, CONFIG, fallback_timezone=TZ) for c in candidates]


def _by_title(title):
    return next(n for n in _normalized() if n.title == title)


# --- detection ---------------------------------------------------------------


def test_detects_confirmed_docs_docs_shape():
    result = SimpleviewEventsDetector().detect(_page1())
    assert result.pattern_name == "simpleview_events"
    assert result.confidence >= 0.6
    assert not result.needs_review
    assert result.evidence["record_path"] == "docs.docs"
    assert result.evidence["id_field"] == "recid"
    assert result.discovered_endpoints == (FIND_URL,)


def test_run_detection_selects_simpleview():
    assert run_detection(_page1()).pattern_name == "simpleview_events"


def test_aggregate_response_does_not_match():
    response = make_response_from_fixture(
        "simpleview_aggregate.json", final_url=FIND_URL, content_type="application/json"
    )
    assert SimpleviewEventsDetector().detect(response).pattern_name is None


def test_generic_nested_json_does_not_false_positive():
    body = '{"docs": {"docs": [{"foo": "bar"}, {"baz": 1}]}}'
    response = make_response(body, final_url=FIND_URL, content_type="application/json")
    assert SimpleviewEventsDetector().detect(response).pattern_name is None


def test_url_text_alone_does_not_match():
    # An HTML page that merely mentions the rest_v2 route is not a match.
    body = "<html><body>Powered by Simpleview rest_v2 plugins_events_events</body></html>"
    response = make_response(body, final_url=FIND_URL, content_type="text/html")
    assert SimpleviewEventsDetector().detect(response).pattern_name is None


def test_empty_docs_handled_honestly():
    response = make_response_from_fixture(
        "simpleview_events_empty.json", final_url=FIND_URL, content_type="application/json"
    )
    result = SimpleviewEventsDetector().detect(response)
    assert result.pattern_name is None
    assert result.needs_review
    assert "simpleview_docs_empty" in result.warnings


# --- registry ----------------------------------------------------------------


def test_pattern_registered_with_proposer():
    assert "simpleview_events" in REGISTRY
    registration = REGISTRY.get("simpleview_events")
    assert registration.has_proposer
    assert registration.classification == "structured"
    from app.extraction.detection import RELIABILITY_ORDER

    assert "simpleview_events" in RELIABILITY_ORDER


# --- proposal ----------------------------------------------------------------


def _propose(request_metadata=None):
    detection = SimpleviewEventsDetector().detect(_page1())
    context = ProposalContext(
        response=_page1(), detection=detection, listing_url=FIND_URL,
        fallback_timezone=TZ, policy=DEFAULT_POLICY, request_metadata=request_metadata,
    )
    return REGISTRY.get("simpleview_events").proposer.propose(context)


def test_proposal_builds_valid_configuration():
    proposal = _propose()
    config = proposal.configuration
    assert config is not None
    assert config.pattern_name == "simpleview_events"
    assert config.json_paths["events_root"] == "docs.docs"
    assert config.timezone == TZ
    assert config.pagination.strategy == "none"
    assert not proposal.missing_required_fields
    # The stable-id note reflects recid.
    assert any("recid" in note for note in proposal.notes)
    # Pagination-unconfirmed warning is explicit, not a silent guess.
    assert any("pagination not confirmed" in w for w in proposal.warnings)


def test_proposal_strips_query_token_from_endpoint():
    detection = SimpleviewEventsDetector().detect(_page1())
    # A discovered endpoint carrying a token must never be persisted verbatim.
    detection = detection.__class__(  # rebuild with a tokened endpoint
        pattern_name=detection.pattern_name, confidence=detection.confidence,
        evidence=detection.evidence, discovered_endpoints=(f"{FIND_URL}?token=SECRET&limit=10",),
        browser_required=False, warnings=detection.warnings,
        detector_version=detection.detector_version, needs_review=detection.needs_review,
    )
    context = ProposalContext(
        response=_page1(), detection=detection, listing_url=FIND_URL,
        fallback_timezone=TZ, policy=DEFAULT_POLICY,
    )
    proposal = REGISTRY.get("simpleview_events").proposer.propose(context)
    assert "token" not in proposal.configuration.api_endpoint
    assert "SECRET" not in proposal.configuration.api_endpoint
    assert any("token" in w for w in proposal.warnings)


def test_proposal_replays_observed_post_body():
    proposal = _propose(
        request_metadata={"method": "POST", "request_body": '{"filter": "upcoming"}'}
    )
    assert proposal.configuration.fetch.method == "POST"
    assert proposal.configuration.fetch.json_body == {"filter": "upcoming"}


def test_proposal_drops_oversized_post_body():
    huge = '{"x": "' + "a" * 5000 + '"}'
    proposal = _propose(request_metadata={"method": "POST", "request_body": huge})
    # Oversized body is not templated; falls back to GET with a warning.
    assert proposal.configuration.fetch.method == "GET"
    assert any("could not be safely templated" in w for w in proposal.warnings)


def _captured_get_metadata():
    """A confirmed GET request with the full URL + safe headers (as the browser
    layer now captures), so the proposer can build a request recipe."""
    query = {
        "filter": {"date_range": {"start": {"$date": "2026-08-06T04:00:00.000Z"},
                                  "end": {"$date": "2026-08-13T03:59:59.999Z"}}},
        "options": {"limit": 100, "skip": 0, "count": True},
    }
    import json as _json
    from urllib.parse import urlencode as _urlencode

    url = FIND_URL + "?" + _urlencode({"json": _json.dumps(query), "token": "PUB-TOKEN-XYZ-123456"})
    return {
        "method": "GET",
        "request_url": url,
        "request_headers": {"Accept": "application/json", "Referer": FIND_URL,
                            "Cookie": "s=SECRET"},
        "request_body": None,
        "query_param_names": ["json", "token"],
        "response_status": 200,
    }


def test_proposal_captures_request_recipe():
    proposal = _propose(request_metadata=_captured_get_metadata())
    recipe = proposal.configuration.request_recipe
    assert recipe is not None
    assert set(recipe.query_params) == {"json", "token"}
    # Public token persisted (it's a request param, not a secret)...
    assert recipe.query_params["token"].value == "PUB-TOKEN-XYZ-123456"
    # ...date range normalized to dynamic placeholders.
    dr = recipe.query_params["json"].value["filter"]["date_range"]
    assert dr["start"] == {"$date": {"kind": "window_start_utc"}}
    # Referer preserved as the dynamic source-page URL; Cookie discarded.
    assert any(v.kind == "source_page_url" for v in recipe.headers.values())
    assert not any(h.lower() == "cookie" for h in recipe.headers)
    assert recipe.pagination.kind == "offset"
    # The "params not persisted" warning is gone — they ARE persisted now.
    assert not any("were not persisted" in w for w in proposal.warnings)
    assert any("captured request recipe" in n for n in proposal.notes)


def test_proposal_without_full_url_keeps_endpoint_only():
    # Backward-compatible: metadata lacking request_url yields no recipe.
    proposal = _propose(request_metadata={"method": "GET", "query_param_names": ["json"]})
    assert proposal.configuration.request_recipe is None
    assert any("pagination not confirmed" in w for w in proposal.warnings)


def test_proposal_recipe_token_redacted_in_notes():
    proposal = _propose(request_metadata=_captured_get_metadata())
    joined = " ".join(proposal.notes)
    assert "PUB-TOKEN-XYZ-123456" not in joined  # never the raw token
    assert "public token" in joined


# --- extraction --------------------------------------------------------------


def test_extraction_skips_non_dict_records():
    # The fixture's trailing non-dict entry is skipped, not crashed on.
    assert len(SimpleviewEventsPattern().extract(_page1(), CONFIG)) == 7


def test_records_read_from_docs_docs_not_first_docs_value():
    # The live envelope nests the record list at docs.docs with a sibling total;
    # `docs` itself is an OBJECT, not the record list. The extractor must read
    # docs.docs, not treat the first `docs` value as the array.
    body = (
        '{"docs": {"docs": ['
        '{"recid": "e1", "title": "Example event", "startDate": "2026-10-06", '
        '"url": "/event/e1/"},'
        '{"recid": "e2", "title": "Second event", "date": "2026-10-07", '
        '"url": "/event/e2/"}'
        '], "total": 12, "count": 12}}'
    )
    response = make_response(body, final_url=FIND_URL, content_type="application/json")
    candidates = SimpleviewEventsPattern().extract(response, CONFIG)
    assert [c.raw.get("title") for c in candidates] == ["Example event", "Second event"]


def test_object_docs_without_nested_docs_yields_no_records():
    # Defensive: if `docs` is an object lacking a `docs` array, no records (never
    # a crash, never mis-reading the envelope object as a record).
    body = '{"docs": {"total": 0}}'
    response = make_response(body, final_url=FIND_URL, content_type="application/json")
    assert SimpleviewEventsPattern().extract(response, CONFIG) == []


def test_required_and_optional_fields_normalized():
    event = _by_title("Autumn Quartet")
    assert event.external_source_id == "sv-1001"  # recid preferred
    assert event.start_date.isoformat() == "2026-10-06"
    assert event.start_time.isoformat() == "19:00:00"
    assert event.end_date.isoformat() == "2026-10-06"
    assert event.timezone == TZ
    assert event.venue == "Buskirk-Chumley Theater"  # location.name, not the object
    assert event.latitude == pytest.approx(39.1653)
    assert event.image_url == "https://cdn.example.org/img/autumn.jpg"
    assert event.source_category == "Music"


def test_relative_url_resolved_absolute_kept():
    assert _by_title("Autumn Quartet").canonical_url.startswith("https://www.visitbloomington.com/")
    assert _by_title("Winter Chorus").canonical_url == (
        "https://visit.example.org/event/winter-chorus/1002/"
    )


def test_categories_deduplicated():
    # Winter Chorus categories were ["Music","Music","Choral"] in source.
    winter = next(
        c for c in SimpleviewEventsPattern().extract(_page1(), CONFIG)
        if c.raw.get("title") == "Winter Chorus"
    )
    assert winter.raw["categories"] == ["Music", "Choral"]


def test_recurrence_preserved_not_expanded():
    winter = next(
        c for c in SimpleviewEventsPattern().extract(_page1(), CONFIG)
        if c.raw.get("title") == "Winter Chorus"
    )
    assert winter.raw["recurrence"] == {"freq": "WEEKLY", "interval": 1}
    assert winter.raw["recur_type"] == "weekly"


def test_date_fallback_when_startdate_absent():
    craft = _by_title("Craft Fair")  # source had `date`, not `startDate`
    assert craft.start_date.isoformat() == "2027-05-01"


def test_unsafe_media_url_ignored():
    craft = _by_title("Craft Fair")  # media_raw was javascript:alert(1)
    assert craft.image_url is None


def test_invalid_records_rejected():
    results = {n.title: validate_candidate(n, CONFIG) for n in _normalized()}
    assert not results["Bad Coordinates"].is_valid
    assert any("latitude" in e for e in results["Bad Coordinates"].errors)
    assert not results["No Date Event"].is_valid
    assert any("start date" in e for e in results["No Date Event"].errors)


def test_duplicate_recid_deduplicated():
    valid = [n for n in _normalized() if validate_candidate(n, CONFIG).is_valid]
    outcome = dedupe_within_run(valid, website_id=4, city_id=None)
    assert outcome.duplicates_skipped == 1  # the repeated recid sv-1001
    kept_ids = {c.external_source_id for c in outcome.kept}
    assert len(kept_ids) == len(outcome.kept)


def test_no_records_from_aggregate_response():
    response = make_response_from_fixture(
        "simpleview_aggregate.json", final_url=FIND_URL, content_type="application/json"
    )
    assert SimpleviewEventsPattern().extract(response, CONFIG) == []


def test_provenance_recorded():
    candidate = SimpleviewEventsPattern().extract(_page1(), CONFIG)[0]
    assert candidate.extraction_pattern == "simpleview_events"
    assert "docs.docs[0].title" in candidate.field_source_paths["title"]
    assert candidate.raw_record_hash


# --- security ----------------------------------------------------------------


def test_unsafe_canonical_url_rejected():
    body = (
        '{"docs": {"docs": [{"recid": "x", "title": "Bad Link", '
        '"startDate": "2026-10-06", "url": "javascript:alert(1)"}]}}'
    )
    response = make_response(body, final_url=FIND_URL, content_type="application/json")
    candidate = SimpleviewEventsPattern().extract(response, CONFIG)[0]
    normalized = normalize_candidate(candidate, CONFIG, fallback_timezone=TZ)
    assert not validate_candidate(normalized, CONFIG).is_valid


def test_arbitrary_method_rejected():
    with pytest.raises(pydantic.ValidationError):
        SiteConfiguration(pattern_name=NAME, api_endpoint=FIND_URL, fetch={"method": "DELETE"})


@pytest.mark.parametrize("header", ["Cookie", "Authorization"])
def test_unsafe_headers_rejected(header):
    with pytest.raises(pydantic.ValidationError):
        SiteConfiguration(
            pattern_name=NAME, api_endpoint=FIND_URL, fetch={"headers": {header: "x"}}
        )


def test_no_hostname_branching_in_module():
    import inspect

    import app.extraction.patterns.simpleview_events as module

    source = inspect.getsource(module).lower()
    assert "visitbloomington" not in source
    assert "bloomington" not in source


# --- recurrence --------------------------------------------------------------

from datetime import date  # noqa: E402

from app.extraction.patterns.simpleview_events import _recurrence_rule  # noqa: E402
from app.extraction.recurrence import expand_candidates  # noqa: E402
from app.schemas.extraction import RecurrenceRuntimeConfig  # noqa: E402

_REC_CONFIG = SiteConfiguration(
    pattern_name=NAME,
    api_endpoint=FIND_URL,
    timezone=TZ,
    recurrence=RecurrenceRuntimeConfig(mode="bounded_expand"),
)


def _recurring_response(recurrence, start, end):
    record = {
        "recid": "evt-1",
        "title": "Karaoke at the Pottery House Studio",
        "url": "https://example.com/events/karaoke",
        "startDate": start,
        "endDate": end,
        "recurrence": recurrence,
        "recurType": 5,
    }
    body = json.dumps({"docs": {"docs": [record]}})
    return make_response(body, final_url=FIND_URL, content_type="application/json")


def test_recurrence_rule_translates_the_sentence_forms():
    assert _recurrence_rule("Recurring daily", None) == "RRULE:FREQ=DAILY"
    assert _recurrence_rule("Recurring weekly on Wednesday", None) == "RRULE:FREQ=WEEKLY;BYDAY=WE"
    assert (
        _recurrence_rule("Recurring weekly on Tuesday, Thursday, Saturday", None)
        == "RRULE:FREQ=WEEKLY;BYDAY=TU,TH,SA"
    )
    assert (
        _recurrence_rule("Recurring monthly on the 3rd Thursday", None)
        == "RRULE:FREQ=MONTHLY;BYDAY=+3TH"
    )
    assert (
        _recurrence_rule("Recurring monthly on the last Friday", None)
        == "RRULE:FREQ=MONTHLY;BYDAY=-1FR"
    )


def test_recurrence_until_is_naive_utc_from_end_date():
    # A tz-aware UNTIL would make dateutil reject the rule against a naive
    # dtstart, so the Z is dropped.
    rule = _recurrence_rule("Recurring daily", "2026-12-18T04:59:59.000Z")
    assert rule == "RRULE:FREQ=DAILY;UNTIL=20261218T045959"


def test_unrecognised_recurrence_is_not_invented():
    assert _recurrence_rule(None, "2026-12-18T00:00:00Z") is None
    assert _recurrence_rule("Recurring every other blue moon", None) is None


def test_recurring_record_becomes_an_rrule_and_drops_the_series_end():
    candidates = SimpleviewEventsPattern().extract(
        _recurring_response(
            "Recurring monthly on the 3rd Thursday",
            "2025-12-18T05:00:00.000Z",
            "2026-12-18T04:59:59.000Z",
        ),
        _REC_CONFIG,
    )
    assert len(candidates) == 1
    recurrence = candidates[0].raw["recurrence"]
    assert isinstance(recurrence, dict)
    assert recurrence["rrule"] == "RRULE:FREQ=MONTHLY;BYDAY=+3TH;UNTIL=20261218T045959"
    # endDate is the series UNTIL now — it must not remain the event's own end,
    # or the parent would span the whole year and read as happening every day.
    assert candidates[0].raw["end_datetime"] is None


def test_monthly_recurrence_expands_to_distinct_third_thursdays():
    candidates = SimpleviewEventsPattern().extract(
        _recurring_response(
            "Recurring monthly on the 3rd Thursday",
            "2025-12-18T05:00:00.000Z",
            "2026-12-18T04:59:59.000Z",
        ),
        _REC_CONFIG,
    )
    normalized = [normalize_candidate(c, _REC_CONFIG, fallback_timezone=TZ) for c in candidates]
    expanded, _ = expand_candidates(normalized, _REC_CONFIG, reference_date=date(2026, 9, 2))
    starts = sorted({c.start_date for c in expanded})
    # Every occurrence is a single third Thursday within the horizon, never one
    # year-long event.
    assert starts == [date(2026, 9, 17), date(2026, 10, 15), date(2026, 11, 19), date(2026, 12, 17)]
    assert all(d.weekday() == 3 for d in starts)  # Thursday
    assert all(c.end_date is None for c in expanded)


def test_non_recurring_record_keeps_its_end_date():
    record = {
        "recid": "evt-2",
        "title": "One-off Gala",
        "url": "https://example.com/events/gala",
        "startDate": "2026-09-10T23:00:00.000Z",
        "endDate": "2026-09-11T03:00:00.000Z",
        "recurrence": None,
        "recurType": 99,
    }
    body = json.dumps({"docs": {"docs": [record]}})
    response = make_response(body, final_url=FIND_URL, content_type="application/json")
    candidate = SimpleviewEventsPattern().extract(response, _REC_CONFIG)[0]
    assert candidate.raw["recurrence"] is None
    assert candidate.raw["end_datetime"] == "2026-09-11T03:00:00.000Z"
