"""Phase 8G: date-range parsing wired through normalization and preview quality."""

from __future__ import annotations

from datetime import date

from app.extraction.inference.quality import evaluate_preview_quality
from app.extraction.normalize import normalize_candidate
from app.extraction.types import EventCandidate, ValidationResult
from app.schemas.extraction import SiteConfiguration

CONFIG = SiteConfiguration(pattern_name="json_ld_event", listing_url="https://example.com/events")


def _candidate(**raw_overrides) -> EventCandidate:
    raw = {"title": "Fair", "canonical_url": "https://example.com/e/1"}
    raw.update(raw_overrides)
    return EventCandidate(
        raw=raw, title=None, canonical_url=None, description=None,
        start_date=None, start_time=None, end_date=None, end_time=None,
        timezone=None, venue=None, address=None, image_url=None,
        latitude=None, longitude=None, source_category=None, external_source_id=None,
        field_source_paths={}, transformation_history=(),
        source_page="https://example.com/events", extraction_pattern="json_ld_event",
        warnings=(), raw_record_hash="deadbeef",
    )


def test_dedicated_date_range_field_fills_start_and_end():
    result = normalize_candidate(_candidate(date_range="Sep 29 - 30 / 2026"), CONFIG)
    assert result.start_date == date(2026, 9, 29)
    assert result.end_date == date(2026, 9, 30)
    assert any(h.startswith("date_range:") for h in result.transformation_history)


def test_range_in_the_start_field_is_parsed_as_a_fallback():
    # A column mixing single dates and ranges: the plain parser fails on the
    # range, and the range parser picks it up without any special config.
    result = normalize_candidate(
        _candidate(start_datetime="September 29 - October 1, 2026"), CONFIG
    )
    assert result.start_date == date(2026, 9, 29)
    assert result.end_date == date(2026, 10, 1)
    assert not any(w.startswith("unparseable_start_date") for w in result.warnings)


def test_explicit_start_date_is_not_overridden_by_range_logic():
    result = normalize_candidate(_candidate(start_datetime="2026-09-29"), CONFIG)
    assert result.start_date == date(2026, 9, 29)
    assert result.end_date is None


def test_ambiguous_range_is_rejected_with_a_warning_not_invented():
    result = normalize_candidate(_candidate(date_range="Sep 29 - 30"), CONFIG)
    assert result.start_date is None
    assert result.end_date is None
    assert any(w == "date_range_ambiguous:missing_year" for w in result.warnings)


def test_preview_quality_reports_range_metrics():
    valid = ValidationResult(is_valid=True, errors=())
    outcomes = [
        (normalize_candidate(_candidate(date_range="Sep 29 - 30 / 2026"), CONFIG), valid),
        (normalize_candidate(_candidate(date_range="Sep 29, 2026 - Oct 1, 2026"), CONFIG), valid),
        (normalize_candidate(_candidate(start_datetime="2026-09-29"), CONFIG), valid),
        (normalize_candidate(_candidate(date_range="Sep 29 - 30"), CONFIG), valid),
    ]
    quality = evaluate_preview_quality(
        outcomes, CONFIG, warnings=[], pages_fetched=1, website_id=1, city_id=None
    )
    # Two multi-day events (both parsed ranges produced an end date).
    assert quality.range_count == 2
    # Three range attempts (two parsed, one ambiguous); two succeeded.
    assert quality.range_parse_success_rate == 2 / 3
    assert quality.ambiguous_range_rejections == 1
    assert quality.end_date_success_rate == 2 / 3


def test_preview_quality_range_success_is_neutral_when_no_ranges():
    valid = ValidationResult(is_valid=True, errors=())
    outcomes = [(normalize_candidate(_candidate(start_datetime="2026-09-29"), CONFIG), valid)]
    quality = evaluate_preview_quality(
        outcomes, CONFIG, warnings=[], pages_fetched=1, website_id=1, city_id=None
    )
    assert quality.range_parse_success_rate == 1.0
    assert quality.end_date_success_rate == 1.0
    assert quality.range_count == 0
