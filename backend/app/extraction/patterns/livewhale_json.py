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
share `recurrence_parent_id`, so each session stays its own listing. An event
counts as repeating when the feed flags it so or lists it with more than one
start time. The same record listed twice (feed pages that overlap) is one event.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.extraction.locations import has_venue
from app.extraction.selectors import resolve_json_path
from app.extraction.types import EventCandidate, FetchResponse
from app.schemas.extraction import SiteConfiguration

NAME = "livewhale_json"
PATTERN_VERSION = "1"

_SESSION_KEY = "livewhale_session"

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


def record_url(api_endpoint: str, event_id: str) -> str | None:
    """The feed URL for one event by its LiveWhale id
    (`/live/json/events/id/<id>/response_fields/...`), on the same host as
    the configured feed. It returns the event even after it has passed and
    left the listing, which is how stored events are rechecked."""
    if not event_id.isdigit():
        return None
    parts = urlsplit(api_endpoint)
    head, found, _ = parts.path.partition(_EVENTS_PATH)
    if not found:
        return None
    fields = "/response_fields/" + ",".join(RESPONSE_FIELDS)
    path = f"{head}{_EVENTS_PATH}/id/{event_id}{fields}"
    return urlunsplit(parts._replace(path=path, query="", fragment=""))


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


def _instant(value: Any) -> datetime | None:
    """The UTC instant a timestamp or offset-carrying ISO value names, or
    None for anything else (a local or date-only value names no instant)."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float) or (isinstance(value, str) and value.strip().isdigit()):
        try:
            return datetime.fromtimestamp(float(value), tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.astimezone(UTC) if parsed.tzinfo is not None else None
    return None


# (offset-aware ISO key, timestamp keys) for each end of the event. The two
# ends are read independently: a start found in `date_iso` says nothing about
# whether the end is in `date2_iso` or only in a timestamp.
_TIME_KEYS: dict[str, tuple[str, tuple[str, ...]]] = {
    "start_datetime": ("date_iso", ("date_ts",)),
    "end_datetime": ("date2_iso", ("date2_ts", "date_end_ts")),
}


def _pick_time(
    event: dict[str, Any],
    field_name: str,
    configured: Any,
    configured_path: str,
    *,
    overridden: bool,
    all_day: bool,
    zone: ZoneInfo | None,
    index: int,
    field_source_paths: dict[str, str],
    warnings: list[str],
) -> Any:
    """One end of the event's time, best source first.

    The offset-carrying ISO value is the source's own local wall clock and is
    preferred whenever it is present. A saved json_path for the field used to
    switch the ISO value off entirely, so a configuration naming `date_ts`
    silently discarded it; now a configured value is used only when the feed
    has no ISO value, and a configured timestamp that names a different
    instant from the ISO value is reported rather than trusted."""
    iso_key, ts_keys = _TIME_KEYS[field_name]
    iso = _iso_value(event.get(iso_key), all_day=all_day)
    iso_instant = _instant(event.get(iso_key))
    if iso is not None:
        checks = [(configured_path, configured)] if overridden else []
        checks += [(key, event.get(key)) for key in ts_keys]
        for path, value in checks:
            other = _instant(value)
            if other is not None and iso_instant is not None and other != iso_instant:
                warnings.append(f"livewhale_time_mismatch:{field_name}:{iso_key}!={path}")
                break
        field_source_paths[field_name] = _provenance_path(index, iso_key)
        return iso
    value, path = configured, configured_path
    if value is None:
        for key in ts_keys:
            if event.get(key) is not None:
                value, path = event.get(key), key
                break
    if value is None:
        return None
    field_source_paths[field_name] = _provenance_path(index, path)
    if isinstance(value, str) and not value.strip().isdigit():
        # A configured ISO-like value: normalize reads its offset (or, for a
        # `*_utc` path, treats it as UTC).
        return value[:10] if all_day else value
    return _timestamp_to_iso(value, all_day=all_day, zone=zone)


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


def _usable(field_name: str, value: Any) -> bool:
    """A value that can stand as this field. A venue written as a placeholder
    ("TBD", "Location TBA") is not one, so it never blocks a real place name
    from `location_title` or a nested place."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return False
    if field_name == "venue":
        return isinstance(value, str) and has_venue({"venue": value})
    return True


def _place_name(value: Any) -> Any:
    if isinstance(value, dict):
        return value.get("title") or value.get("name")
    return value


def _fill_place_fallbacks(
    event: dict[str, Any],
    raw: dict[str, Any],
    field_source_paths: dict[str, str],
    index: int,
) -> None:
    venue = raw.get("venue")
    if isinstance(venue, dict):
        # A nested place object: its name is the venue.
        name = _place_name(venue)
        raw["venue"] = name if isinstance(name, str) else None
        field_source_paths["venue"] = _provenance_path(index, "location.title")
    for field_name, keys in _PLACE_FALLBACKS.items():
        if _usable(field_name, raw.get(field_name)):
            continue
        for key in keys:
            value = _place_name(event.get(key))
            if _usable(field_name, value):
                raw[field_name] = value
                field_source_paths[field_name] = _provenance_path(index, key)
                break
        else:
            if field_name == "venue" and raw.get("venue") is not None:
                # Only a placeholder was given: no venue, so the event's own
                # page is still looked up for one.
                raw["venue"] = None


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

    def finalize_candidates(
        self, candidates: list[EventCandidate], config: SiteConfiguration
    ) -> list[EventCandidate]:
        """Session keys decided across the whole run, not page by page. A
        series whose sessions straddle two feed pages would otherwise be keyed
        by its bare id on one page and "<id>@<date>" on the other, and both
        would match the same stored row."""
        return _key_sessions(candidates)

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
            warnings: list[str] = []
            for field_name in _TIME_KEYS:
                raw[field_name] = _pick_time(
                    event,
                    field_name,
                    raw.get(field_name),
                    paths[field_name],
                    overridden=field_name in overridden,
                    all_day=all_day,
                    zone=zone,
                    index=index,
                    field_source_paths=field_source_paths,
                    warnings=warnings,
                )
            start = raw["start_datetime"]
            end = raw["end_datetime"]
            if end is None and "date2_iso" in event:
                # The feed states the end and it is empty: the event has no end
                # time, so one stored by an earlier, misread run is stale.
                raw["end_absent"] = True

            # Every session of a repeating event shares its `id`: each session
            # is keyed by id and local date (see _key_sessions) so none is
            # collapsed into another.
            event_id = raw.get("event_id")
            if (
                event.get("occur_id") is None
                and event_id is not None
                and isinstance(start, str)
                and len(start) >= 10
            ):
                raw[_SESSION_KEY] = [
                    str(event_id), start[:10], bool(event.get("repeats")), start,
                ]

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
                    raw_record_hash=raw_record_hash,
                    recurrence_parent_id=raw.get("recurrence_parent_id"),
                    is_cancelled=bool(event.get("is_canceled")),
                    warnings=tuple(warnings),
                )
            )
        return _key_sessions(candidates)


def _key_sessions(candidates: list[EventCandidate]) -> list[EventCandidate]:
    """Key each session of a repeating event "<id>@<local date>"; a single
    event keeps its bare id. Repeating means the feed says so, or the same id
    has more than one start time among these records. The same record listed
    twice (overlapping feed pages) has one start time, so it stays one event
    under its bare id instead of becoming a one-session series."""
    starts: dict[str, set[str]] = {}
    for candidate in candidates:
        session = candidate.raw.get(_SESSION_KEY)
        if session:
            starts.setdefault(session[0], set()).add(_session_start(session))
    keyed: list[EventCandidate] = []
    for candidate in candidates:
        session = candidate.raw.get(_SESSION_KEY)
        if not session:
            keyed.append(candidate)
            continue
        event_id, day, repeats = session[0], session[1], session[2]
        raw = dict(candidate.raw)
        if repeats or len(starts[event_id]) > 1:
            raw["external_source_id"] = f"{event_id}@{day}"
            raw["recurrence_parent_id"] = event_id
        else:
            raw["external_source_id"] = event_id
            raw["recurrence_parent_id"] = None
        keyed.append(replace(candidate, raw=raw, recurrence_parent_id=raw["recurrence_parent_id"]))
    return keyed


def _session_start(session: list) -> str:
    # Older session markers carried only the date.
    return str(session[3]) if len(session) > 3 else str(session[1])
