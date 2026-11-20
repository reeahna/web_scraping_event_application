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

Times. The `/live/json/events` feed gives each start as `date_iso` (the
event's local wall clock with its UTC offset, e.g. "2026-11-19T20:00:00-05:00")
and as `date_ts` (unix seconds); the end is `date2_iso`/`date2_ts` (older
installs used `date_end_ts`). The offset-carrying ISO value is preferred, and a
timestamp is emitted as an explicit UTC instant, so app.extraction.normalize
can place it on the event's own wall clock. (Reading the timestamp as a naive
UTC time is what used to turn an 8 PM show into "1 AM the next day", and a
midnight-UTC start into "Time TBD".) An all-day event (`is_all_day`) is a
calendar date only, never a UTC midnight.

Detail fields. The feed leaves out location, description, image, tags and
event types unless they are named in a `/response_fields/...` path segment;
request_url() adds that segment to a bare `/live/json/events` endpoint, which
is why IU events used to show "Venue TBD" although the event pages had one.

Sessions. LiveWhale lists every session of a repeating event as its own
object sharing the event `id`. Keying them all by that id collapsed a series
to one row; a repeating event's sessions are keyed `<id>@<local date>` and
share `recurrence_parent_id`, so each session stays its own listing.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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


# Detail fields the feed only returns when asked for them by name.
RESPONSE_FIELDS = (
    "location", "summary", "description", "image", "event_types", "tags",
    "group_title", "status",
)
_EVENTS_PATH = "/live/json/events"


def request_url(url: str) -> str:
    """A bare `/live/json/events` endpoint with the response_fields segment
    added, so the feed includes location, description and image. Any URL that
    already names its fields, or is not that endpoint, is returned unchanged."""
    parts = urlsplit(url)
    path = parts.path.rstrip("/")
    if "/response_fields/" in path or _EVENTS_PATH not in path:
        return url
    head, _, tail = path.partition(_EVENTS_PATH)
    fields = "/response_fields/" + ",".join(RESPONSE_FIELDS)
    return urlunsplit(parts._replace(path=f"{head}{_EVENTS_PATH}{fields}{tail}"))


def _zone(name: Any) -> ZoneInfo | None:
    if not isinstance(name, str) or not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def _timestamp_to_iso(value: Any, *, all_day: bool, zone: ZoneInfo | None = None) -> Any:
    """A unix timestamp as an explicit UTC instant ("...+00:00"), which
    normalize converts to the event's zone. An all-day value is the calendar
    date in the event's own zone (`zone`), falling back to UTC."""
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
        return dt.astimezone(zone).date().isoformat() if zone else dt.date().isoformat()
    return dt.isoformat()


def _iso_value(value: Any, *, all_day: bool) -> Any:
    """`date_iso`/`date2_iso` as given (local time with its offset), or just
    its date for an all-day event."""
    if not isinstance(value, str) or len(value) < 10:
        return None
    return value[:10] if all_day else value


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


def _first_name(value: Any) -> Any:
    if isinstance(value, str):
        return value or None
    return _first_group_name(value)


def _first_group_name(value: Any) -> Any:
    if not isinstance(value, list) or not value:
        return None
    first = value[0]
    if isinstance(first, dict):
        return first.get("name") or first.get("title")
    if isinstance(first, str):
        return first
    return None


# LiveWhale fills `location` only for a free-text location. An event linked
# to one of the calendar's Places carries it as `location_title` (with
# `location_latitude`/`location_longitude`), and some installs nest it as a
# `location` object instead. Without these the venue was dropped and the event
# showed "Venue TBD".
_PLACE_FALLBACKS: dict[str, tuple[str, ...]] = {
    "venue": ("location_title", "location_name", "place_title", "place"),
    "latitude": ("location_latitude", "location_lat"),
    "longitude": ("location_longitude", "location_lng", "location_lon"),
}


def _fill_place_fallbacks(
    event: dict[str, Any],
    raw: dict[str, Any],
    field_source_paths: dict[str, str],
    index: int,
) -> None:
    venue = raw.get("venue")
    if isinstance(venue, dict):
        # A nested place object: its name is the venue.
        name = venue.get("title") or venue.get("name")
        raw["venue"] = name if isinstance(name, str) else None
        field_source_paths["venue"] = _provenance_path(index, "location.title")
    for field_name, keys in _PLACE_FALLBACKS.items():
        current = raw.get(field_name)
        if current is not None and not (isinstance(current, str) and not current.strip()):
            continue
        for key in keys:
            value = event.get(key)
            if isinstance(value, dict):
                value = value.get("title") or value.get("name")
            if value is not None and not (isinstance(value, str) and not value.strip()):
                raw[field_name] = value
                field_source_paths[field_name] = _provenance_path(index, key)
                break


def _events_from_payload(payload: Any) -> list[Any] | None:
    if isinstance(payload, dict):
        # LiveWhale exposes the event list under `events` on some installs and
        # under `data` (a JSON:API-style envelope with `meta`/`links`/`data`)
        # on others — e.g. the `/live/json/events` feed on events.iu.edu.
        for key in ("events", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        return None
    if isinstance(payload, list):
        return payload
    return None


class LiveWhalePattern:
    name = NAME

    def request_url(self, url: str, config: SiteConfiguration) -> str:
        return request_url(url)

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

        id_counts: dict[str, int] = {}
        for event in events:
            if isinstance(event, dict) and event.get("id") is not None:
                key = str(event["id"])
                id_counts[key] = id_counts.get(key, 0) + 1
        overridden = set(config.json_paths)

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

            _fill_place_fallbacks(event, raw, field_source_paths, index)

            if raw.get("external_source_id") is None:
                raw["external_source_id"] = raw.get("event_id")
            if raw.get("external_source_id") is not None:
                raw["external_source_id"] = str(raw["external_source_id"])

            all_day = bool(raw.get("all_day") or event.get("is_all_day"))
            raw["all_day"] = all_day
            zone = _zone(event.get("timezone")) or _zone(config.timezone)
            start = end = None
            if "start_datetime" not in overridden:
                start = _iso_value(event.get("date_iso"), all_day=all_day)
                if start is not None:
                    field_source_paths["start_datetime"] = _provenance_path(index, "date_iso")
            if start is None:
                start = _timestamp_to_iso(raw.get("start_datetime"), all_day=all_day, zone=zone)
            if "end_datetime" not in overridden:
                end = _iso_value(event.get("date2_iso"), all_day=all_day)
                if end is not None:
                    field_source_paths["end_datetime"] = _provenance_path(index, "date2_iso")
                elif raw.get("end_datetime") is None and event.get("date2_ts") is not None:
                    raw["end_datetime"] = event.get("date2_ts")
                    field_source_paths["end_datetime"] = _provenance_path(index, "date2_ts")
            if end is None:
                end = _timestamp_to_iso(raw.get("end_datetime"), all_day=all_day, zone=zone)
            raw["start_datetime"] = start
            raw["end_datetime"] = end

            # Every session of a repeating event shares its `id`: key each
            # session by id and local date so none is collapsed into another.
            event_id = raw.get("event_id")
            repeating = bool(event.get("repeats")) or (
                event_id is not None and id_counts.get(str(event_id), 0) > 1
            )
            if (
                repeating
                and event.get("occur_id") is None
                and event_id is not None
                and isinstance(start, str)
                and len(start) >= 10
            ):
                raw["external_source_id"] = f"{event_id}@{start[:10]}"
                raw["recurrence_parent_id"] = str(event_id)

            raw["image"] = _extract_image(raw.get("image") or event.get("thumbnail"))
            raw["source_category"] = (
                _first_group_name(raw.get("groups"))
                or _first_name(event.get("event_types"))
                or _first_name(event.get("group_title"))
            )

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
                    recurrence_parent_id=raw.get("recurrence_parent_id"),
                    is_cancelled=bool(event.get("is_canceled")),
                )
            )
        return candidates
