import dataclasses
from datetime import date

from app.extraction.normalize import normalize_candidate
from app.extraction.transform import apply_transformations, parse_date_value, parse_time_value
from app.extraction.types import EventCandidate
from app.extraction.validate import validate_candidate
from app.schemas.extraction import SiteConfiguration, TransformationRuleConfig

CONFIG = SiteConfiguration(pattern_name="json_ld_event", listing_url="https://example.com/events")


def _raw_candidate(**raw_overrides) -> EventCandidate:
    raw = {
        "title": "  A Title  ",
        "canonical_url": "/events/x",
        "description": "<p>Hello <script>alert(1)</script>world</p>",
        "start_datetime": "2025-06-01",
        "end_datetime": None,
        "start_time": None,
        "end_time": None,
        "venue": "  Some   Venue  ",
        "address": None,
        "image": "/img/x.jpg",
        "source_category": None,
        "external_source_id": None,
    }
    raw.update(raw_overrides)
    return EventCandidate(
        raw=raw,
        title=None,
        canonical_url=None,
        description=None,
        start_date=None,
        start_time=None,
        end_date=None,
        end_time=None,
        timezone=None,
        venue=None,
        address=None,
        image_url=None,
        latitude=None,
        longitude=None,
        source_category=None,
        external_source_id=None,
        field_source_paths={},
        transformation_history=(),
        source_page="https://example.com/events",
        extraction_pattern="json_ld_event",
        warnings=(),
        raw_record_hash="deadbeef",
    )


def test_whitespace_normalization_on_title_and_venue():
    normalized = normalize_candidate(_raw_candidate(), CONFIG)
    assert normalized.title == "A Title"
    assert normalized.venue == "Some Venue"


def test_unicode_normalization_transformation():
    rules = [
        TransformationRuleConfig(field="title", kind="unicode_normalize", params={"form": "NFKC"})
    ]
    result, history = apply_transformations("café", rules)  # e + combining acute
    assert result == "café"
    assert history == ["unicode_normalize"]


def test_relative_urls_converted_to_absolute():
    normalized = normalize_candidate(_raw_candidate(), CONFIG)
    assert normalized.canonical_url == "https://example.com/events/x"
    assert normalized.image_url == "https://example.com/img/x.jpg"


def test_deterministic_date_parsing_repeated_calls_match():
    first = parse_date_value("June 1, 2025", ["%B %d, %Y"])
    second = parse_date_value("June 1, 2025", ["%B %d, %Y"])
    assert first == second == date(2025, 6, 1)


def test_date_parsing_falls_back_to_iso_when_no_format_matches():
    assert parse_date_value("2025-06-01", []) == date(2025, 6, 1)


def test_unparseable_date_produces_warning_not_a_crash():
    normalized = normalize_candidate(_raw_candidate(start_datetime="not a date"), CONFIG)
    assert normalized.start_date is None
    assert any("unparseable_start_date" in w for w in normalized.warnings)


def test_configured_site_timezone_applied_when_no_explicit_offset():
    config = SiteConfiguration(
        pattern_name="json_ld_event",
        listing_url="https://example.com/events",
        timezone="America/Chicago",
    )
    normalized = normalize_candidate(_raw_candidate(), config)
    assert normalized.timezone == "America/Chicago"


def test_explicit_offset_flagged_as_conflicting_when_site_timezone_also_configured():
    config = SiteConfiguration(
        pattern_name="json_ld_event",
        listing_url="https://example.com/events",
        timezone="America/Chicago",
    )
    normalized = normalize_candidate(
        _raw_candidate(start_datetime="2025-06-01T19:00:00-05:00"), config
    )
    assert any("conflicting_timezone_info" in w for w in normalized.warnings)
    assert normalized.start_time is not None
    assert normalized.start_time.tzinfo is None  # stored as naive wall-clock time


def test_never_invents_missing_optional_fields():
    normalized = normalize_candidate(_raw_candidate(venue=None, address=None, image=None), CONFIG)
    assert normalized.venue is None
    assert normalized.address is None
    assert normalized.image_url is None


def test_description_sanitized_of_script_tags():
    normalized = normalize_candidate(_raw_candidate(), CONFIG)
    assert "<script" not in (normalized.description or "").lower()
    assert "world" in normalized.description


def test_invalid_coordinates_rejected():
    candidate = _raw_candidate()
    normalized = normalize_candidate(candidate, CONFIG)
    normalized = dataclasses.replace(normalized, latitude=200.0, longitude=0.0)
    result = validate_candidate(normalized, CONFIG)
    assert not result.is_valid
    assert any("latitude" in err for err in result.errors)


def test_missing_required_field_rejected():
    config = SiteConfiguration(
        pattern_name="json_ld_event",
        listing_url="https://example.com/events",
        required_fields=["title", "start_date", "canonical_url", "venue"],
    )
    normalized = normalize_candidate(_raw_candidate(venue=None), config)
    result = validate_candidate(normalized, config)
    assert not result.is_valid
    assert any("venue" in err for err in result.errors)


def test_one_invalid_candidate_does_not_affect_another_valid_one():
    valid = normalize_candidate(_raw_candidate(), CONFIG)
    invalid = normalize_candidate(_raw_candidate(title=None), CONFIG)
    valid_result = validate_candidate(valid, CONFIG)
    invalid_result = validate_candidate(invalid, CONFIG)
    assert valid_result.is_valid
    assert not invalid_result.is_valid


def test_time_parsing_accepts_iso_time_with_offset():
    parsed = parse_time_value("19:00:00-05:00", [])
    assert parsed is not None
    assert parsed.hour == 19
