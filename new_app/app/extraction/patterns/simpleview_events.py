"""Simpleview events API extraction pattern.

Simpleview powers many destination-marketing (DMO / "Visit X") sites. Its
event API returns a JSON object whose events live at a nested record array —
by default `docs.docs` — where each record carries a stable platform id
(`recid`, falling back to `_id`), a `title`, ISO `startDate`/`endDate`, a
detail `url`, and optional `location`/`city`/coordinates/`categories`/
`media_raw`/`recurrence` fields.

This is a *generic, configuration-driven* pattern — nothing here references a
hostname, domain, or institution. Field mapping is `config.json_paths`
(same convention as wordpress_rest / livewhale_json / algolia_search); the
record-array root is `config.json_paths["events_root"]` (default `docs.docs`,
like algolia's `hits`). As with every pattern, `extract` fills only `raw` +
provenance and leaves typed fields None — `app.extraction.normalize` parses
dates, resolves/validates URLs, and types coordinates in exactly one place.

Recurrence fields are preserved verbatim in `raw` for provenance; they are
never expanded here (bounded expansion is `app.extraction.recurrence`'s job,
gated by `config.recurrence`).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.extraction.selectors import resolve_json_path
from app.extraction.types import EventCandidate, FetchResponse
from app.schemas.extraction import SiteConfiguration

NAME = "simpleview_events"
PATTERN_VERSION = "1"

# Default nested location of the event array. Overridable via
# `config.json_paths["events_root"]`, so a Simpleview deployment exposing a
# differently-nested array needs configuration, not code.
EVENTS_ROOT_DEFAULT = "docs.docs"

# Stable-id candidates, most-preferred first. A scalar platform id is always
# preferred over deriving identity from title+date (see dedup).
_ID_PATHS = ("recid", "_id", "id")

_DEFAULT_PATHS: dict[str, str] = {
    "title": "title",
    "description": "description",
    "canonical_url": "url",
    "start_datetime": "startDate",
    "end_datetime": "endDate",
    # A `date` field is used as the start when `startDate` is absent.
    "date": "date",
    "venue": "location",
    "locality": "city",
    "latitude": "latitude",
    "longitude": "longitude",
    "categories": "categories",
    "image": "media_raw",
    "recurrence": "recurrence",
    "recur_type": "recurType",
}
_RAW_FIELDS = ("events_root", *_DEFAULT_PATHS.keys())

_IMAGE_DICT_KEYS = ("url", "src", "lg", "large", "original", "md", "medium", "sm", "small", "uri")
_VENUE_NAME_KEYS = ("name", "title", "venue", "label", "displayName")
# Dangerous URL schemes an image string must never carry. Full SSRF/public-URL
# validation still happens downstream; this drops the obviously-unsafe ones so a
# malformed media URL is ignored rather than promoted to an image.
_UNSAFE_URL_PREFIXES = ("javascript:", "data:", "file:", "vbscript:", "blob:")


def _safe_url_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped:
        return None
    lowered = stripped.lower()
    if any(lowered.startswith(prefix) for prefix in _UNSAFE_URL_PREFIXES):
        return None
    return stripped


def _stable_id(record: dict[str, Any]) -> str | None:
    """The first present scalar platform id. Never derives identity from
    title/date — dedup will fall back to the canonical URL when no id exists."""
    for key in _ID_PATHS:
        value = record.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value).strip()
    return None


def _venue_name(value: Any) -> Any:
    """A venue *name*, never a whole nested object stringified. Accepts a plain
    string, or an object exposing a recognisable name field."""
    if isinstance(value, str):
        return value or None
    if isinstance(value, dict):
        for key in _VENUE_NAME_KEYS:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
        return None
    if isinstance(value, list) and value:
        return _venue_name(value[0])
    return None


def _extract_image(value: Any) -> Any:
    """A single safe image URL from `media_raw` without assuming its schema.
    Unwraps strings, url-bearing dicts, and lists; obviously-unsafe URLs
    (javascript:/data:/file:/…) are ignored rather than promoted to an image."""
    if isinstance(value, str):
        return _safe_url_string(value)
    if isinstance(value, dict):
        for key in _IMAGE_DICT_KEYS:
            candidate = _safe_url_string(value.get(key))
            if candidate:
                return candidate
        return None
    if isinstance(value, list) and value:
        return _extract_image(value[0])
    return None


def _category_names(value: Any) -> list[str]:
    """Deduplicated category names, order-preserving, from a list of strings or
    name-bearing objects (provenance of the full list stays in `raw`)."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    names: list[str] = []
    for item in value:
        name: str | None = None
        if isinstance(item, str):
            name = item.strip() or None
        elif isinstance(item, dict):
            for key in ("name", "title", "label", "category"):
                candidate = item.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    name = candidate.strip()
                    break
        if name and name not in names:
            names.append(name)
    return names


class SimpleviewEventsPattern:
    name = NAME

    def extract(self, response: FetchResponse, config: SiteConfiguration) -> list[EventCandidate]:
        try:
            payload = json.loads(response.text)
        except (json.JSONDecodeError, ValueError):
            return []
        if not isinstance(payload, dict):
            return []

        events_root = config.json_paths.get("events_root", EVENTS_ROOT_DEFAULT)
        resolved = resolve_json_path(payload, events_root).value
        records = [r for r in resolved if isinstance(r, dict)] if isinstance(resolved, list) else []

        paths = {
            **_DEFAULT_PATHS,
            **{k: v for k, v in config.json_paths.items() if k in _RAW_FIELDS},
        }

        candidates: list[EventCandidate] = []
        for index, record in enumerate(records):
            raw: dict[str, Any] = {}
            field_source_paths: dict[str, str] = {}
            for field_name, path in paths.items():
                if field_name == "events_root":
                    continue
                result = resolve_json_path(record, path)
                raw[field_name] = result.value
                field_source_paths[field_name] = f"{NAME}.{events_root}[{index}].{path}"

            # Stable platform id (recid -> _id -> id), never title+date.
            raw["external_source_id"] = _stable_id(record)
            field_source_paths["external_source_id"] = f"{NAME}.{events_root}[{index}]"

            # `startDate` preferred; fall back to `date` only when absent.
            if not raw.get("start_datetime") and raw.get("date"):
                raw["start_datetime"] = raw["date"]

            raw["venue"] = _venue_name(raw.get("venue"))
            raw["image"] = _extract_image(raw.get("image"))

            category_names = _category_names(raw.get("categories"))
            raw["categories"] = category_names or None
            raw["source_category"] = category_names[0] if category_names else None

            # City is the only confirmed locality-ish string; surface it as the
            # address rather than stringifying an unknown nested object.
            locality = raw.get("locality")
            raw["address"] = locality if isinstance(locality, str) and locality else None

            raw_record_hash = hashlib.sha256(
                json.dumps(record, sort_keys=True, default=str).encode("utf-8")
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
