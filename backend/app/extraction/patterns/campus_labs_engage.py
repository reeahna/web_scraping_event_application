"""Campus Labs Engage event-discovery extraction pattern.

Engage (the student-organization platform many universities run, on a
`<school>.campuslabs.com/engage` address or the school's own domain) renders its
events page in the browser from a public discovery API:

    GET {base}/api/discovery/event/search?endsAfter=...&take=..&skip=..
    -> {"@odata.count": 412, "value": [{"id": "1234", "name": "...",
        "startsOn": "2026-10-12T22:00:00+00:00", "endsOn": "...",
        "location": "...", "description": "<p>...</p>", "imagePath": "abc...",
        "categoryNames": [...], "organizationName": "...", ...}]}

`{base}` is the page origin, plus `/engage` when the site lives under that path.
Event pages are `{base}/event/{id}`. Both are platform route conventions, not
facts about any one school. The request is a captured `RequestRecipe` so the
`endsAfter` boundary is computed fresh on every run and `skip`/`take` walk the
pages.

As with every pattern, `extract` fills only `raw` + provenance; the one
conversion done here is turning the API's UTC timestamps into local wall-clock
times (see paged_json_events.local_datetime_string).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from app.extraction.patterns.paged_json_events import local_datetime_string
from app.extraction.types import EventCandidate, FetchResponse
from app.schemas.extraction import SiteConfiguration

NAME = "campus_labs_engage"
PATTERN_VERSION = "1"

SEARCH_ROUTE = "api/discovery/event/search"
# The platform's public image CDN; `imagePath` is a key on it.
IMAGE_URL_TEMPLATE = "https://se-images.campuslabs.com/clink/images/{path}?preset=med-w"
# Markers of an Engage page in its own markup: the platform's static-asset
# host, and the app-state global its pages boot from.
PAGE_MARKERS = ("campuslabsengage.com", "window.initialappstate")
_RECORD_KEYS = frozenset({"startsOn", "endsOn", "name", "id"})
_PLATFORM_KEYS = frozenset({"organizationName", "organizationId", "institutionId", "imagePath"})


def engage_base(url: str) -> str:
    """The Engage base URL for a page or API URL on an Engage site: the origin,
    plus `/engage` when the site lives under that path."""
    parts = urlsplit(url)
    prefix = "/engage" if parts.path.lower().startswith("/engage") else ""
    return urlunsplit((parts.scheme, parts.netloc, prefix, "", ""))


def search_endpoint(url: str) -> str:
    return f"{engage_base(url)}/{SEARCH_ROUTE}"


def looks_like_engage_records(payload: Any) -> list[dict] | None:
    """The `value` records of an Engage discovery response, or None."""
    if not isinstance(payload, dict) or not isinstance(payload.get("value"), list):
        return None
    records = [r for r in payload["value"] if isinstance(r, dict)]
    sample = records[:5]
    if sample and not all(
        set(r) >= _RECORD_KEYS and _PLATFORM_KEYS & set(r) for r in sample
    ):
        return None
    if not sample and "@odata.count" not in payload:
        return None
    return records


def _image(path: Any) -> str | None:
    if not isinstance(path, str) or not path.strip():
        return None
    text = path.strip()
    if text.lower().startswith(("http://", "https://")):
        return text
    return IMAGE_URL_TEMPLATE.format(path=quote(text, safe=""))


def _names(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    names: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip() and item.strip() not in names:
            names.append(item.strip())
    return names


class CampusLabsEngagePattern:
    name = NAME

    def extract(self, response: FetchResponse, config: SiteConfiguration) -> list[EventCandidate]:
        try:
            payload = json.loads(response.text)
        except (json.JSONDecodeError, ValueError):
            return []
        records = looks_like_engage_records(payload) or []
        base = engage_base(config.api_endpoint or response.final_url)

        candidates: list[EventCandidate] = []
        for index, record in enumerate(records):
            record_id = record.get("id")
            record_id = str(record_id).strip() if record_id not in (None, "") else None
            categories = _names(record.get("categoryNames"))
            raw: dict[str, Any] = {
                "external_source_id": record_id,
                "title": record.get("name"),
                "description": record.get("description"),
                "start_datetime": local_datetime_string(record.get("startsOn"), config.timezone),
                "end_datetime": local_datetime_string(record.get("endsOn"), config.timezone),
                "canonical_url": f"{base}/event/{quote(record_id)}" if record_id else None,
                "venue": record.get("location") if isinstance(record.get("location"), str)
                else None,
                "image": _image(record.get("imagePath")),
                "latitude": record.get("latitude"),
                "longitude": record.get("longitude"),
                "categories": categories or None,
                "source_category": categories[0] if categories else None,
                "organizer": record.get("organizationName"),
            }
            field_source_paths = {
                field: f"{NAME}.value[{index}].{source}"
                for field, source in (
                    ("external_source_id", "id"), ("title", "name"),
                    ("description", "description"), ("start_datetime", "startsOn"),
                    ("end_datetime", "endsOn"), ("canonical_url", "id"),
                    ("venue", "location"), ("image", "imagePath"),
                    ("categories", "categoryNames"),
                )
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
