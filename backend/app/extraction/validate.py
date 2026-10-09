"""EventValidator: independent per-candidate validation.

One invalid candidate never aborts the whole extraction run — each
candidate is validated on its own, and rejected candidates remain visible
through ExtractionRun/ExtractionError diagnostics rather than being
silently dropped.
"""

from __future__ import annotations

from app.core.url_safety import UnsafeURLError, validate_public_url
from app.extraction.types import EventCandidate, ValidationResult
from app.schemas.extraction import SiteConfiguration

_FIELD_PRESENCE_CHECKS = {
    "title": lambda c: c.title,
    "start_date": lambda c: c.start_date,
    "canonical_url": lambda c: c.canonical_url,
    "venue": lambda c: c.venue,
    "address": lambda c: c.address,
    "description": lambda c: c.description,
    "external_source_id": lambda c: c.external_source_id,
}

_ALWAYS_REQUIRED = ("title", "start_date")


def validate_candidate(candidate: EventCandidate, config: SiteConfiguration) -> ValidationResult:
    errors: list[str] = []

    if not candidate.title:
        errors.append("title is required")
    if candidate.start_date is None:
        errors.append("a parseable start date is required")

    for field_name in config.required_fields:
        if field_name in _ALWAYS_REQUIRED:
            continue  # already checked unconditionally above, don't duplicate
        check = _FIELD_PRESENCE_CHECKS.get(field_name)
        if check is not None and not check(candidate):
            errors.append(f"required field missing: {field_name}")

    if candidate.canonical_url:
        try:
            validate_public_url(candidate.canonical_url)
        except UnsafeURLError as exc:
            errors.append(f"invalid canonical_url: {exc}")

    if candidate.start_date is not None and candidate.end_date is not None:
        if candidate.end_date < candidate.start_date:
            errors.append("end_date is before start_date")
        elif (
            candidate.end_date == candidate.start_date
            and candidate.start_time is not None
            and candidate.end_time is not None
            and candidate.end_time < candidate.start_time
        ):
            errors.append("end_time is before start_time on the same date")

    if candidate.latitude is not None and not (-90.0 <= candidate.latitude <= 90.0):
        errors.append("latitude out of range")
    if candidate.longitude is not None and not (-180.0 <= candidate.longitude <= 180.0):
        errors.append("longitude out of range")
    if (candidate.latitude is None) != (candidate.longitude is None):
        errors.append("latitude and longitude must both be present or both be absent")

    if candidate.description and "<script" in candidate.description.lower():
        errors.append("description contains unsanitized script content")

    return ValidationResult(is_valid=not errors, errors=tuple(errors))
