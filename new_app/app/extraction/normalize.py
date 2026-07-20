"""EventNormalizer: deterministic conversion from raw extracted values to
typed, cleaned fields.

Never invents a missing title, date, time, venue, address, coordinates,
category, or canonical URL — only applies configured fallback behavior
(e.g. `allow_page_url_as_canonical_fallback`, already resolved by the
pattern before this runs). No LLM call, no geocoding, anywhere in this
module or this engine.
"""

from __future__ import annotations

import dataclasses
from urllib.parse import urljoin

from app.extraction.sanitize import strip_to_text
from app.extraction.transform import apply_transformations, parse_date_value, parse_time_value
from app.extraction.types import EventCandidate
from app.schemas.extraction import SiteConfiguration, TransformationRuleConfig


def _clean_whitespace(value: object) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text or None


def _rules_for_field(
    rules: list[TransformationRuleConfig], field_name: str
) -> list[TransformationRuleConfig]:
    return [r for r in rules if r.field == field_name]


def _split_iso_datetime(value: str | None) -> tuple[str | None, str | None]:
    if not value:
        return None, None
    if "T" in value:
        date_part, _, time_part = value.partition("T")
        return date_part or None, time_part or None
    return value, None


def normalize_candidate(
    candidate: EventCandidate,
    config: SiteConfiguration,
    *,
    fallback_timezone: str | None = None,
) -> EventCandidate:
    """Deterministic: identical `candidate.raw` + `config` +
    `fallback_timezone` always produces an identical normalized
    EventCandidate — required for the engine's repeated-run determinism
    guarantee."""
    raw = candidate.raw
    history: list[str] = list(candidate.transformation_history)
    warnings: list[str] = list(candidate.warnings)

    def apply(field_name: str, value: object) -> object:
        rules = _rules_for_field(config.transformations, field_name)
        if not rules:
            return value
        result, applied = apply_transformations(value, rules)
        history.extend(applied)
        return result

    title = _clean_whitespace(apply("title", raw.get("title")))

    description_raw = apply("description", raw.get("description"))
    # Templates never render descriptions with a `|safe` filter (confirmed:
    # no such usage exists anywhere in this codebase), so a tag-preserving
    # sanitized value would just display as literal escaped markup. Plain
    # text is what the current rendering path actually needs.
    description = strip_to_text(description_raw) if description_raw is not None else None

    venue = _clean_whitespace(apply("venue", raw.get("venue")))
    address = _clean_whitespace(apply("address", raw.get("address")))
    source_category = _clean_whitespace(apply("source_category", raw.get("source_category")))

    external_source_id = apply("external_source_id", raw.get("external_source_id"))
    if external_source_id is not None:
        external_source_id = str(external_source_id).strip() or None

    canonical_url = apply("canonical_url", raw.get("canonical_url"))
    if canonical_url:
        canonical_url = urljoin(candidate.source_page, str(canonical_url))

    image_url = apply("image", raw.get("image"))
    if image_url:
        image_url = urljoin(candidate.source_page, str(image_url))

    start_date_part, start_time_from_dt = _split_iso_datetime(
        apply("start_datetime", raw.get("start_datetime"))
    )
    end_date_part, end_time_from_dt = _split_iso_datetime(
        apply("end_datetime", raw.get("end_datetime"))
    )
    start_time_raw = apply("start_time", raw.get("start_time")) or start_time_from_dt
    end_time_raw = apply("end_time", raw.get("end_time")) or end_time_from_dt

    start_date = parse_date_value(start_date_part, config.date_formats)
    end_date = parse_date_value(end_date_part, config.date_formats)
    start_time = parse_time_value(start_time_raw, config.time_formats)
    end_time = parse_time_value(end_time_raw, config.time_formats)

    if start_date_part is not None and start_date is None:
        warnings.append(f"unparseable_start_date:{start_date_part}")
    if end_date_part is not None and end_date is None:
        warnings.append(f"unparseable_end_date:{end_date_part}")

    timezone = config.timezone or fallback_timezone
    for label, parsed_time in (("start_time", start_time), ("end_time", end_time)):
        if parsed_time is not None and parsed_time.tzinfo is not None and timezone is not None:
            warnings.append(f"conflicting_timezone_info:{label}")
        if parsed_time is not None and parsed_time.tzinfo is not None:
            # Store naive wall-clock time as extracted; the explicit source
            # offset isn't an IANA zone name and can't be losslessly stored
            # in the `timezone` string column, so it's recorded as a warning
            # rather than silently discarded.
            if label == "start_time":
                start_time = parsed_time.replace(tzinfo=None)
            else:
                end_time = parsed_time.replace(tzinfo=None)

    latitude = _parse_coordinate(raw.get("latitude"), warnings, "latitude")
    longitude = _parse_coordinate(raw.get("longitude"), warnings, "longitude")

    return dataclasses.replace(
        candidate,
        title=title,
        canonical_url=canonical_url,
        description=description,
        start_date=start_date,
        start_time=start_time,
        end_date=end_date,
        end_time=end_time,
        timezone=timezone,
        venue=venue,
        address=address,
        image_url=image_url,
        latitude=latitude,
        longitude=longitude,
        source_category=source_category,
        external_source_id=external_source_id,
        transformation_history=tuple(history),
        warnings=tuple(warnings),
    )


def _parse_coordinate(value: object, warnings: list[str], label: str) -> float | None:
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        warnings.append(f"unparseable_{label}:{value}")
        return None
