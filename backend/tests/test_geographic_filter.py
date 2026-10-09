"""Phase 8G: the shared geographic filter service."""

from __future__ import annotations

from app.extraction.types import EventCandidate
from app.schemas.geographic import (
    BoundingBoxRule,
    GeographicFilterConfig,
    RadiusRule,
)
from app.services.geographic_filter import apply_geographic_filter


def _candidate(**over) -> EventCandidate:
    base = dict(
        address=None, venue=None, latitude=None, longitude=None,
    )
    base.update(over)
    return EventCandidate(
        raw={}, title="E", canonical_url="https://x/e", description=None,
        start_date=None, start_time=None, end_date=None, end_time=None,
        timezone=None, venue=base["venue"], address=base["address"], image_url=None,
        latitude=base["latitude"], longitude=base["longitude"],
        source_category=None, external_source_id=None, field_source_paths={},
        transformation_history=(), source_page="https://x", extraction_pattern="p",
        warnings=(), raw_record_hash="h",
    )


def test_no_filter_configured_includes_everything():
    decision = apply_geographic_filter(_candidate(address="Anywhere"), None)
    assert decision.included is True
    assert decision.geography_missing is False


def test_locality_matches_on_word_boundary():
    config = GeographicFilterConfig(localities=["Springfield"])
    inside = apply_geographic_filter(
        _candidate(address="100 Main St, Springfield, IL 62704"), config
    )
    assert inside.included is True
    assert "localities" in inside.matched_rules

    outside = apply_geographic_filter(_candidate(address="Chicago, IL"), config)
    assert outside.included is False
    assert outside.outcome == "excluded"


def test_locality_does_not_match_a_substring_of_another_word():
    config = GeographicFilterConfig(localities=["York"])
    # "Yorkshire" must not satisfy a "York" rule.
    decision = apply_geographic_filter(_candidate(address="Yorkshire Dales"), config)
    assert decision.included is False


def test_aliases_broaden_matching_without_fuzz():
    config = GeographicFilterConfig(
        localities=["St. Louis"], aliases={"St. Louis": ["Saint Louis", "STL"]}
    )
    decision = apply_geographic_filter(_candidate(address="Downtown Saint Louis MO"), config)
    assert decision.included is True


def test_postal_code_and_prefix():
    config = GeographicFilterConfig(postal_codes=["62704"], postal_code_prefixes=["627"])
    assert apply_geographic_filter(_candidate(address="x, 62704"), config).included is True
    assert apply_geographic_filter(_candidate(address="x, 62799"), config).included is True
    assert apply_geographic_filter(_candidate(address="x, 90210"), config).included is False


def test_radius_uses_coordinates_and_never_geocodes():
    config = GeographicFilterConfig(
        radius=RadiusRule(center_latitude=39.78, center_longitude=-89.65, radius_km=20)
    )
    near = apply_geographic_filter(_candidate(latitude=39.80, longitude=-89.64), config)
    assert near.included is True
    far = apply_geographic_filter(_candidate(latitude=41.88, longitude=-87.63), config)
    assert far.included is False
    # No coordinates: the radius rule is unevaluable, so geography counts as
    # missing rather than silently included.
    no_coords = apply_geographic_filter(_candidate(address="Springfield"), config)
    assert no_coords.geography_missing is True


def test_bounding_box():
    config = GeographicFilterConfig(
        bounding_box=BoundingBoxRule(
            min_latitude=39.0, max_latitude=40.0, min_longitude=-90.0, max_longitude=-89.0
        )
    )
    assert apply_geographic_filter(
        _candidate(latitude=39.5, longitude=-89.5), config
    ).included is True
    assert apply_geographic_filter(
        _candidate(latitude=42.0, longitude=-89.5), config
    ).included is False


def test_all_mode_requires_every_group():
    config = GeographicFilterConfig(
        mode="all", localities=["Springfield"], regions=["IL"]
    )
    both = apply_geographic_filter(_candidate(address="Springfield, IL"), config)
    assert both.included is True
    one = apply_geographic_filter(_candidate(address="Springfield, MO"), config)
    assert one.included is False


def test_missing_geography_actions():
    base = dict(localities=["Springfield"])
    reject = GeographicFilterConfig(missing_geography_action="reject", **base)
    keep = GeographicFilterConfig(missing_geography_action="keep_with_warning", **base)
    review = GeographicFilterConfig(missing_geography_action="needs_review", **base)
    empty = _candidate()  # no address, no coords

    d_reject = apply_geographic_filter(empty, reject)
    assert d_reject.included is False and d_reject.outcome == "missing_reject"

    d_keep = apply_geographic_filter(empty, keep)
    assert d_keep.included is True and d_keep.outcome == "missing_keep"

    d_review = apply_geographic_filter(empty, review)
    assert d_review.included is True and d_review.needs_review is True


def test_inclusion_is_never_inferred_from_assigned_city():
    # The candidate has no geography of its own; even though a real run would
    # have assigned it to a city, the filter only sees the candidate and must
    # treat it as missing, not included-by-city.
    config = GeographicFilterConfig(localities=["Springfield"])
    decision = apply_geographic_filter(_candidate(), config)
    assert decision.geography_missing is True
    assert "localities" not in decision.matched_rules
