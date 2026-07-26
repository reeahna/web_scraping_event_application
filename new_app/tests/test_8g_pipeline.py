"""Phase 8G: recurrence expansion and geographic annotation as pipeline steps."""

from __future__ import annotations

from datetime import date, time

from app.extraction.recurrence import expand_candidates
from app.extraction.types import EventCandidate
from app.schemas.extraction import RecurrenceRuntimeConfig, SiteConfiguration
from app.schemas.geographic import GeographicFilterConfig
from app.schemas.recurrence import RecurrenceBounds
from app.services.geographic_filter import (
    annotate_candidate_geography,
    geo_needs_review,
    geo_should_drop,
)

REF = date(2026, 1, 1)


def _candidate(**over) -> EventCandidate:
    base = dict(
        start_date=date(2026, 3, 2), start_time=time(19, 0), end_date=None, end_time=None,
        external_source_id="UID-1", address=None, venue=None, latitude=None, longitude=None,
        raw={},
    )
    base.update(over)
    return EventCandidate(
        raw=base["raw"], title="Show", canonical_url="https://x/e",
        description=None, start_date=base["start_date"], start_time=base["start_time"],
        end_date=base["end_date"], end_time=base["end_time"], timezone=None,
        venue=base["venue"], address=base["address"], image_url=None,
        latitude=base["latitude"], longitude=base["longitude"],
        source_category=None, external_source_id=base["external_source_id"],
        field_source_paths={}, transformation_history=(), source_page="https://x",
        extraction_pattern="ics_calendar", warnings=(), raw_record_hash="h1",
    )


def _config(**over) -> SiteConfiguration:
    values = {"pattern_name": "ics_calendar", "listing_url": "https://x/feed.ics"}
    values.update(over)
    return SiteConfiguration(**values)


def test_parent_only_config_leaves_candidates_untouched():
    cand = _candidate(raw={"recurrence": {"rrule": "FREQ=WEEKLY;COUNT=3"}})
    out, warnings = expand_candidates([cand], _config(), reference_date=REF)
    assert out == [cand]
    assert warnings == []


def test_bounded_expand_produces_distinct_occurrences():
    cand = _candidate(raw={"recurrence": {"rrule": "FREQ=WEEKLY;COUNT=3"}})
    config = _config(recurrence=RecurrenceRuntimeConfig(mode="bounded_expand"))
    out, _ = expand_candidates([cand], config, reference_date=REF)
    assert len(out) == 3
    ids = {c.occurrence_id for c in out}
    assert len(ids) == 3  # each occurrence has a distinct identity
    # Dedup identity is carried on external_source_id, never the shared parent UID.
    assert all(c.external_source_id == c.occurrence_id for c in out)
    assert {c.start_date for c in out} == {date(2026, 3, 2), date(2026, 3, 9), date(2026, 3, 16)}


def test_run_budget_caps_total_occurrences():
    cands = [
        _candidate(external_source_id=f"UID-{i}", raw={"recurrence": {"rrule": "FREQ=DAILY"}})
        for i in range(3)
    ]
    config = _config(
        recurrence=RecurrenceRuntimeConfig(
            mode="bounded_expand",
            bounds=RecurrenceBounds(horizon_days=400, max_occurrences_per_run=10),
        )
    )
    out, warnings = expand_candidates(cands, config, reference_date=REF)
    assert len(out) == 10
    assert "recurrence_run_budget_exceeded" in warnings


def test_a_candidate_without_recurrence_passes_through():
    cand = _candidate(raw={})  # no recurrence payload
    config = _config(recurrence=RecurrenceRuntimeConfig(mode="bounded_expand"))
    out, _ = expand_candidates([cand], config, reference_date=REF)
    assert out == [cand]


def test_geo_annotation_marks_drop_and_review():
    config = GeographicFilterConfig(
        localities=["Springfield"], missing_geography_action="needs_review"
    )
    included = annotate_candidate_geography(_candidate(address="Springfield, IL"), config)
    excluded = annotate_candidate_geography(_candidate(address="Chicago, IL"), config)
    missing = annotate_candidate_geography(_candidate(address=None), config)

    assert not geo_should_drop(included) and not geo_needs_review(included)
    assert geo_should_drop(excluded)
    assert geo_needs_review(missing) and not geo_should_drop(missing)


def test_geo_annotation_is_a_noop_without_a_filter():
    cand = _candidate(address="Anywhere")
    assert annotate_candidate_geography(cand, None) is cand
