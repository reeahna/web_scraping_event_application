"""LiveWhale calendar JSON API extraction pattern.

Fetches `config.api_endpoint` expecting LiveWhale's own JSON shape —
`{"events": [...], "count": ...}` or a bare list of event objects. Field
mapping is config-driven (`config.json_paths`), same convention as
wordpress_rest/json_ld_event/the_events_calendar.

Only fields with a persisted Event column (title, description, canonical_url,
start/end datetime, timezone, venue, address, image, source_category,
external_source_id) are promoted to EventCandidate's typed fields. Everything
else LiveWhale returns but this engine has no typed slot for yet (summary,
tags, groups, contact info, individual locality/region/postal components,
the parent event id alongside its per-occurrence id) is preserved in `raw` +
`field_source_paths` for audit/provenance — never fabricated, never silently
dropped.

`date_ts`/`date_end_ts` are unix timestamps (seconds since epoch, per
LiveWhale's own API convention) — converted here to the same naive
wall-clock ISO string shape every other pattern in this engine produces.
This pipeline never performs timezone math anywhere (config.timezone only
ever *labels* the parsed value — see app.extraction.normalize), so this is a
UTC-instant-as-wall-clock conversion, not a shift into the site's local
timezone. Documented limitation, not a silent approximation.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from app.extraction.selectors import resolve_json_path
from app.extraction.types import EventCandidate, FetchResponse
from app.schemas.extraction import SiteConfiguration

NAME = "livewhale_json"
PATTERN_VERSION = "1"

_DEFAULT_PATHS: dict[str, str] = {
    # The occurrence's own id — dedup-safe for recurring events, same
    # precedent as the_events_calendar's "id" (each occurrence is its own
    # object). Falls back to the parent "id" below when absent.
    "external_source_id": "occur_id",
    "event_id": "id",
    "title": "title",
    "canonical_url": "url",
    "start_datetime": "date_ts",
    "end_datetime": "date_end_ts",
    "all_day": "all_day",
    "timezone": "timezone",
    "venue": "location",
    "address": "address",
    "locality": "city",
    "region": "state",
    "postal_code": "zip",
    "latitude": "latitude",
    "longitude": "longitude",
    "summary": "summary",
    "description": "description",
    "image": "photo",
    "tags": "tags",
    "groups": "groups",
    "contact_name": "contact_name",
    "contact_email": "contact_email",
    "contact_phone": "contact_phone",
    # Not part of LiveWhale's public API as far as this pattern assumes —
    # preserved verbatim, never parsed/expanded, only when a deployment
    # happens to expose it under this key.
    "recurrence": "recurrence",
}

_RAW_FIELDS = tuple(_DEFAULT_PATHS.keys())

_IMAGE_DICT_KEYS = ("lg", "large", "original", "url", "src", "md", "medium", "sm", "small")


def _provenance_path(index: int, relative_path: str) -> str:
    return f"livewhale.events[{index}].{relative_path}"


def _timestamp_to_iso(value: Any, *, all_day: bool) -> Any:
    if value is None:
        return None
    try:
        ts = float(value)
    except (TypeError, ValueError):
        # Left as-is: not a crash, just fails to parse as a date downstream
        # (the existing required-start-date validation rejects the record).
        return value
    dt = datetime.fromtimestamp(ts, tz=UTC)
    if all_day:
        return dt.date().isoformat()
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _extract_image(value: Any) -> Any:
    if isinstance(value, str):
        return value or None
    if isinstance(value, dict):
        for key in _IMAGE_DICT_KEYS:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
        return None
    if isinstance(value, list) and value:
        return _extract_image(value[0])
    return None


def _first_group_name(value: Any) -> Any:
    if not isinstance(value, list) or not value:
        return None
    first = value[0]
    if isinstance(first, dict):
        return first.get("name") or first.get("title")
    if isinstance(first, str):
        return first
    return None


def _events_from_payload(payload: Any) -> list[Any] | None:
    if isinstance(payload, dict):
        events = payload.get("events")
        return events if isinstance(events, list) else None
    if isinstance(payload, list):
        return payload
    return None


class LiveWhalePattern:
    name = NAME

    def extract(self, response: FetchResponse, config: SiteConfiguration) -> list[EventCandidate]:
        try:
            payload = json.loads(response.text)
        except json.JSONDecodeError:
            return []

        events = _events_from_payload(payload)
        if events is None:
            return []

        paths = {
            **_DEFAULT_PATHS,
            **{k: v for k, v in config.json_paths.items() if k in _RAW_FIELDS},
        }

        candidates: list[EventCandidate] = []
        for index, event in enumerate(events):
            if not isinstance(event, dict):
                continue

            raw: dict[str, Any] = {}
            field_source_paths: dict[str, str] = {}
            for field_name, path in paths.items():
                result = resolve_json_path(event, path)
                raw[field_name] = result.value
                field_source_paths[field_name] = _provenance_path(index, path)

            if raw.get("external_source_id") is None:
                raw["external_source_id"] = raw.get("event_id")
            if raw.get("external_source_id") is not None:
                raw["external_source_id"] = str(raw["external_source_id"])

            all_day = bool(raw.get("all_day"))
            raw["start_datetime"] = _timestamp_to_iso(raw.get("start_datetime"), all_day=all_day)
            raw["end_datetime"] = _timestamp_to_iso(raw.get("end_datetime"), all_day=all_day)

            raw["image"] = _extract_image(raw.get("image"))
            raw["source_category"] = _first_group_name(raw.get("groups"))

            address_parts = [
                raw.get("address"),
                raw.get("locality"),
                raw.get("region"),
                raw.get("postal_code"),
            ]
            composed_address = ", ".join(str(p) for p in address_parts if p)
            raw["address"] = composed_address or None

            raw_record_hash = hashlib.sha256(
                json.dumps(event, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()

            candidates.append(
                EventCandidate(
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
                    field_source_paths=field_source_paths,
                    transformation_history=(),
                    source_page=response.final_url,
                    extraction_pattern=NAME,
                    warnings=(),
                    raw_record_hash=raw_record_hash,
                )
            )
        return candidates
