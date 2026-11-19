"""BiblioCommons library events extraction pattern.

Public libraries on BiblioCommons (`<library>.bibliocommons.com/events`) render
their events page in the browser from a public gateway API:

    GET https://gateway.bibliocommons.com/v2/libraries/<library>/events
        ?locale=en-US&limit=100&page=1
    -> {"events": {"items": ["<id>", ...],
                   "pagination": {"count": 485, "pages": 5, "page": 1, "limit": 100}},
        "entities": {"events": {"<id>": {"id": "...", "definition": {
                         "title": "...", "start": "2026-12-15T18:30",
                         "end": "2026-12-15T19:45", "description": "<p>...</p>",
                         "branchLocationId": "19", "nonBranchLocationId": null,
                         "typeIds": [...], "featuredImageId": "...",
                         "isCancelled": false}}},
                     "locations": {"19": {"name": "Main Library", "address": {...}}},
                     "places": {...}, "eventTypes": {...}, "images": {...}}}

The page lists event ids; each event, its venue (a library branch in
`locations` or another place in `places`), its type and its image are looked up
by id in `entities`. `start`/`end` are already local wall-clock times.

The library name comes from the API path, and event pages follow the platform's
`https://<library>.bibliocommons.com/events/<id>` route. Both are platform
conventions, not facts about any one library. Pages are walked with the
`path_page` pagination strategy (a template ending in `page={page}`).

As with every pattern, `extract` fills only `raw` + provenance. Cancelled
events are skipped.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from urllib.parse import urlsplit

from app.extraction.types import EventCandidate, FetchResponse
from app.schemas.extraction import SiteConfiguration

NAME = "bibliocommons_events"
PATTERN_VERSION = "1"

GATEWAY_TEMPLATE = (
    "https://gateway.bibliocommons.com/v2/libraries/{library}/events?locale=en-US&limit=100"
)
EVENT_URL_TEMPLATE = "https://{library}.bibliocommons.com/events/{id}"
_LIBRARY_IN_PATH = re.compile(r"/libraries/([A-Za-z0-9_-]+)/events")
_LIBRARY_HOST = re.compile(r"^([A-Za-z0-9_-]+)\.bibliocommons\.com$", re.IGNORECASE)


def library_from_url(url: str) -> str | None:
    """The library's short name from a gateway API URL or a library page URL."""
    parts = urlsplit(url)
    match = _LIBRARY_IN_PATH.search(parts.path)
    if match:
        return match.group(1)
    host = _LIBRARY_HOST.match(parts.hostname or "")
    if host and host.group(1).lower() not in ("gateway", "www"):
        return host.group(1)
    return None


def gateway_endpoint(library: str) -> str:
    return GATEWAY_TEMPLATE.format(library=library)


def records(payload: Any) -> list[dict] | None:
    """The page's event records in list order, or None when the payload is not
    a BiblioCommons events response."""
    if not isinstance(payload, dict):
        return None
    events = payload.get("events")
    entities = payload.get("entities")
    if not isinstance(events, dict) or not isinstance(entities, dict):
        return None
    items = events.get("items")
    by_id = entities.get("events")
    if not isinstance(items, list) or not isinstance(by_id, dict):
        return None
    out: list[dict] = []
    for item in items:
        record = by_id.get(str(item))
        if isinstance(record, dict) and isinstance(record.get("definition"), dict):
            out.append(record)
    return out


def _entity(entities: dict, kind: str, entity_id: Any) -> dict | None:
    table = entities.get(kind)
    if entity_id is None or not isinstance(table, dict):
        return None
    value = table.get(str(entity_id))
    return value if isinstance(value, dict) else None


def _address(place: dict | None) -> str | None:
    if not place:
        return None
    address = place.get("address")
    if not isinstance(address, dict):
        return None
    street = " ".join(
        str(p) for p in (address.get("number"), address.get("street")) if p
    ).strip()
    region = " ".join(str(p) for p in (address.get("state"), address.get("zip")) if p).strip()
    parts = [p for p in (street, address.get("city"), region) if p]
    return ", ".join(str(p) for p in parts) or None


def _image(entities: dict, image_id: Any) -> str | None:
    image = _entity(entities, "images", image_id)
    if not image:
        return None
    sizes = image.get("sizes")
    if isinstance(sizes, list):
        by_key = {s.get("key"): s.get("url") for s in sizes if isinstance(s, dict)}
        for key in ("large", "medium", "full"):
            if isinstance(by_key.get(key), str):
                return by_key[key]
    url = image.get("url")
    return url if isinstance(url, str) and url else None


class BiblioCommonsEventsPattern:
    name = NAME

    def extract(self, response: FetchResponse, config: SiteConfiguration) -> list[EventCandidate]:
        try:
            payload = json.loads(response.text)
        except (json.JSONDecodeError, ValueError):
            return []
        found = records(payload)
        if not found:
            return []
        entities = payload["entities"]
        library = library_from_url(response.final_url) or library_from_url(
            config.api_endpoint or ""
        )

        candidates: list[EventCandidate] = []
        for index, record in enumerate(found):
            definition = record["definition"]
            if definition.get("isCancelled"):
                continue
            event_id = record.get("id")
            prefix = f"bibliocommons.entities.events[{event_id}].definition"

            venue_path = "branchLocationId"
            place = _entity(entities, "locations", definition.get("branchLocationId"))
            if place is None:
                venue_path = "nonBranchLocationId"
                place = _entity(entities, "places", definition.get("nonBranchLocationId"))
            name = place.get("name") if place else None
            type_ids = definition.get("typeIds") or []
            event_type = _entity(entities, "eventTypes", type_ids[0]) if type_ids else None
            map_location = (place or {}).get("mapLocation") or {}
            centre = map_location.get("centrePoint") if isinstance(map_location, dict) else None
            centre = centre if isinstance(centre, dict) else {}

            raw: dict[str, Any] = {
                "external_source_id": str(event_id) if event_id is not None else None,
                "title": definition.get("title"),
                "description": definition.get("description"),
                "start_datetime": definition.get("start"),
                "end_datetime": definition.get("end"),
                "venue": name.strip() if isinstance(name, str) else None,
                "address": _address(place),
                "latitude": centre.get("lat"),
                "longitude": centre.get("lng"),
                "image": _image(entities, definition.get("featuredImageId")),
                "source_category": (event_type or {}).get("name"),
                "canonical_url": (
                    EVENT_URL_TEMPLATE.format(library=library, id=event_id)
                    if library and event_id
                    else None
                ),
                "series_id": record.get("seriesId"),
            }
            field_source_paths = {
                "external_source_id": f"bibliocommons.events.items[{index}]",
                "title": f"{prefix}.title",
                "description": f"{prefix}.description",
                "start_datetime": f"{prefix}.start",
                "end_datetime": f"{prefix}.end",
                "venue": f"{prefix}.{venue_path}",
                "address": f"{prefix}.{venue_path}",
                "image": f"{prefix}.featuredImageId",
                "source_category": f"{prefix}.typeIds[0]",
                "canonical_url": "platform route /events/{id}",
            }
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
