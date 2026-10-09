"""Preview scoring.

Turns the outcome of a preview run into the numbers an approval decision is
actually made on. Pure: it is handed already-extracted candidates and their
validation results, so it never fetches, never persists, and can be exercised
directly from fixtures.
"""

from __future__ import annotations

from app.core.url_safety import UnsafeURLError, validate_public_url
from app.extraction.dedup import dedupe_within_run
from app.extraction.inference.types import PreviewQualityResult
from app.extraction.types import EventCandidate, ValidationResult
from app.schemas.extraction import SiteConfiguration

_FIELD_ACCESSORS = {
    "title": lambda c: c.title,
    "start_date": lambda c: c.start_date,
    "canonical_url": lambda c: c.canonical_url,
    "venue": lambda c: c.venue,
    "address": lambda c: c.address,
    "description": lambda c: c.description,
    "image_url": lambda c: c.image_url,
    "external_source_id": lambda c: c.external_source_id,
}

_DETAIL_SOURCE_PREFIX = "detail:"
_RANGE_HISTORY_PREFIX = "date_range:"
_AMBIGUOUS_RANGE_PREFIX = "date_range_ambiguous:"
# The geographic decision the pipeline records as provenance. Duplicated here
# (rather than imported from app.services) to keep the inference layer free of
# a dependency on the service layer.
_GEO_HISTORY_PREFIX = "geographic:"
_GEO_DROP_OUTCOMES = frozenset({"excluded", "missing_reject"})


def _ratio(count: int, total: int) -> float:
    return count / total if total else 0.0


def _geo_outcome(candidate) -> str | None:
    for token in reversed(candidate.transformation_history):
        if token.startswith(_GEO_HISTORY_PREFIX):
            return token[len(_GEO_HISTORY_PREFIX):]
    return None


def _url_is_valid(url: str | None) -> bool:
    if not url:
        return False
    try:
        validate_public_url(url)
    except UnsafeURLError:
        return False
    return True


def evaluate_preview_quality(
    outcomes: list[tuple[EventCandidate, ValidationResult]],
    config: SiteConfiguration,
    *,
    warnings: list[str],
    pages_fetched: int,
    website_id: int,
    city_id: int | None,
) -> PreviewQualityResult:
    total = len(outcomes)
    candidates = [candidate for candidate, _ in outcomes]
    valid = [candidate for candidate, result in outcomes if result.is_valid]
    rejected = total - len(valid)

    coverage = {
        field_name: _ratio(
            sum(1 for c in candidates if _FIELD_ACCESSORS[field_name](c)),
            total,
        )
        for field_name in config.required_fields
        if field_name in _FIELD_ACCESSORS
    }

    duplicates = dedupe_within_run(valid, website_id=website_id, city_id=city_id)

    # `max_events_reached` is appended by the pipeline itself when it stops
    # early; hitting the page cap is the other truncation shape.
    truncated = "max_events_reached" in warnings or (
        pages_fetched > 0 and pages_fetched >= config.pagination.max_pages
    )
    detail_fetches = sum(
        1
        for c in candidates
        if any(path.startswith(_DETAIL_SOURCE_PREFIX) for path in c.field_source_paths.values())
    )

    # Date-range metrics (Phase 8G). A candidate "attempted" a range parse if
    # the normalizer stamped either a success token in its history or an
    # ambiguous-rejection warning; a value it simply couldn't read at all is
    # not counted as a range attempt.
    range_attempts = range_parsed = ambiguous_rejections = end_dates_in_attempts = 0
    for c in candidates:
        parsed = any(h.startswith(_RANGE_HISTORY_PREFIX) for h in c.transformation_history)
        ambiguous = any(w.startswith(_AMBIGUOUS_RANGE_PREFIX) for w in c.warnings)
        if parsed:
            range_attempts += 1
            range_parsed += 1
            if c.end_date is not None:
                end_dates_in_attempts += 1
        elif ambiguous:
            range_attempts += 1
            ambiguous_rejections += 1
    range_count = sum(1 for c in candidates if c.end_date is not None)
    range_parse_success_rate = _ratio(range_parsed, range_attempts) if range_attempts else 1.0
    end_date_success_rate = _ratio(end_dates_in_attempts, range_attempts) if range_attempts else 1.0

    # Geographic-filter metrics, read from the provenance the pipeline stamped.
    geo_considered = geo_included = geo_excluded = geo_missing = 0
    for c in candidates:
        outcome = _geo_outcome(c)
        if outcome is None:
            continue
        geo_considered += 1
        if outcome.startswith("missing"):
            geo_missing += 1
        if outcome in _GEO_DROP_OUTCOMES:
            geo_excluded += 1
        else:
            geo_included += 1
    geo_inclusion_rate = _ratio(geo_included, geo_considered) if geo_considered else 1.0

    return PreviewQualityResult(
        candidates_found=total,
        valid_count=len(valid),
        rejected_count=rejected,
        valid_percentage=_ratio(len(valid), total),
        rejected_percentage=_ratio(rejected, total),
        required_field_coverage=coverage,
        date_parse_success_rate=_ratio(
            sum(1 for c in candidates if c.start_date is not None), total
        ),
        url_validity_rate=_ratio(
            sum(1 for c in candidates if _url_is_valid(c.canonical_url)), total
        ),
        duplicate_rate=_ratio(duplicates.duplicates_skipped, len(valid)),
        warning_count=len(warnings) + sum(len(c.warnings) for c in candidates),
        pagination_truncated=truncated,
        detail_fetch_used=detail_fetches > 0,
        pages_fetched=pages_fetched,
        range_count=range_count,
        range_parse_success_rate=range_parse_success_rate,
        end_date_success_rate=end_date_success_rate,
        ambiguous_range_rejections=ambiguous_rejections,
        geographic_considered=geo_considered,
        geographic_included=geo_included,
        geographic_excluded=geo_excluded,
        geographic_missing=geo_missing,
        geographic_inclusion_rate=geo_inclusion_rate,
    )


def meets_approval_bar(
    quality: PreviewQualityResult, policy, *, preview_status: str
) -> tuple[bool, list[str]]:
    """The preview half of the ready_for_approval gate. Returns (ok, reasons
    it isn't) so the UI can state exactly which bar was missed rather than
    just refusing."""
    reasons: list[str] = []
    if preview_status not in ("success", "partial"):
        reasons.append(f"preview status was '{preview_status}'")
    if quality.valid_count < policy.min_valid_candidates:
        reasons.append(
            f"only {quality.valid_count} valid candidates "
            f"(minimum {policy.min_valid_candidates})"
        )
    if quality.valid_percentage < policy.min_valid_percentage:
        reasons.append(
            f"valid percentage {quality.valid_percentage:.0%} "
            f"below {policy.min_valid_percentage:.0%}"
        )
    if quality.date_parse_success_rate < policy.min_date_parse_success:
        reasons.append(
            f"date parse success {quality.date_parse_success_rate:.0%} "
            f"below {policy.min_date_parse_success:.0%}"
        )
    if quality.url_validity_rate < policy.min_url_validity:
        reasons.append(
            f"URL validity {quality.url_validity_rate:.0%} below {policy.min_url_validity:.0%}"
        )
    if quality.duplicate_rate > policy.max_duplicate_rate:
        reasons.append(
            f"duplicate rate {quality.duplicate_rate:.0%} above {policy.max_duplicate_rate:.0%}"
        )
    min_range = getattr(policy, "min_range_parse_success", 0.0)
    if min_range and quality.range_parse_success_rate < min_range:
        reasons.append(
            f"date-range parse success {quality.range_parse_success_rate:.0%} "
            f"below {min_range:.0%}"
        )
    return not reasons, reasons
