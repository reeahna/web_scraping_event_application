"""The Events Calendar REST API extraction pattern (tribe/events/v1).

Fetches `config.api_endpoint` expecting the plugin's own JSON shape —
either `{"events": [...], "total_pages": ..., "next_rest_url": ...}` or a
bare list of event objects. Field mapping is config-driven
(`config.json_paths`), same convention as wordpress_rest/json_ld_event.

Only `title`, `description`, `canonical_url`, `start_datetime`,
`end_datetime`, `timezone`, `venue`, `address`, `image`, `source_category`,
`external_source_id` are promoted to EventCandidate's typed fields — those
are the only fields with a persisted Event column. Fields the source
returns but the engine has no typed slot for yet (excerpt, organizer,
cost, categories, recurrence, series id, individual locality/region/postal
components) are preserved in `raw` + `field_source_paths` for audit and
provenance, never fabricated, never silently dropped.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from app.extraction.selectors import resolve_json_path
from app.extraction.types import EventCandidate, FetchResponse
from app.schemas.extraction import SiteConfiguration

NAME = "the_events_calendar"
PATTERN_VERSION = "1"

_DEFAULT_PATHS: dict[str, str] = {
    "external_source_id": "id",
    "title": "title",
    "description": "description",
    "excerpt": "excerpt",
    "canonical_url": "url",
    "start_datetime": "start_date",
    "end_datetime": "end_date",
    "all_day": "all_day",
    "timezone": "timezone",
    "image": "image.url",
    "venue": "venue.venue",
    "address": "venue.address",
    "locality": "venue.city",
    "region": "venue.state",
    "postal_code": "venue.zip",
    "latitude": "venue.geo_lat",
    "longitude": "venue.geo_lng",
    "organizer": "organizer[0].organizer",
    "categories": "categories",
    "source_category": "categories[0].name",
    "cost": "cost",
    "recurrence": "recurrence",
    "series_id": "series",
}

_RAW_FIELDS = tuple(_DEFAULT_PATHS.keys())

_TRIBE_DATETIME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})$")


def _provenance_path(index: int, relative_path: str) -> str:
    return f"tribe.events[{index}].{relative_path}"


def _to_iso_datetime(value: Any, *, all_day: bool) -> Any:
    """Tribe's own `start_date`/`end_date` fields are `YYYY-MM-DD HH:MM:SS`
    (space-separated, not ISO 8601) — converted here, inside this pattern,
    to the `YYYY-MM-DDTHH:MM:SS` shape app.extraction.normalize already
    knows how to split. For an all-day event the source's midnight
    timestamp isn't a real scheduled time, so only the date is kept."""
    if not isinstance(value, str):
        return value
    match = _TRIBE_DATETIME_RE.match(value.strip())
    if not match:
        return value
    date_part, time_part = match.groups()
    return date_part if all_day else f"{date_part}T{time_part}"


def _events_from_payload(payload: Any) -> list[Any] | None:
    if isinstance(payload, dict):
        events = payload.get("events")
        return events if isinstance(events, list) else None
    if isinstance(payload, list):
        return payload
    return None


class TheEventsCalendarPattern:
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

            if raw.get("external_source_id") is not None:
                raw["external_source_id"] = str(raw["external_source_id"])

            all_day = bool(raw.get("all_day"))
            raw["start_datetime"] = _to_iso_datetime(raw.get("start_datetime"), all_day=all_day)
            raw["end_datetime"] = _to_iso_datetime(raw.get("end_datetime"), all_day=all_day)

            # Tribe returns `false` (not null) for an unset image.
            if raw.get("image") is False:
                raw["image"] = None

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
