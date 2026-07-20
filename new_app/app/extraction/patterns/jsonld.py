"""JSON-LD schema.org Event extraction pattern.

Supports: a single Event object, arrays of Event objects, `@graph`, nested
Event objects, schema.org Event subtypes (matched via `@type` containing
"event", case-insensitively — so "MusicEvent"/"SportsEvent"/"TheaterEvent"
etc. are picked up without a per-subtype conditional), `image` as
string/object/array, `location` as Place or VirtualLocation, `address` as a
string or PostalAddress object, `organizer`.

Only produces *raw* candidates (see patterns/base.py) — no date/time
parsing, no URL resolution happens here. The page's own URL is never used as
`canonical_url` unless `config.allow_page_url_as_canonical_fallback` is set;
`offers.url` is never used as the event URL unless
`config.allow_offers_url_as_event_url` is set.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from bs4 import BeautifulSoup

from app.extraction.selectors import resolve_json_path
from app.extraction.types import EventCandidate, FetchResponse
from app.schemas.extraction import SiteConfiguration

NAME = "json_ld_event"
PATTERN_VERSION = "1"

_DEFAULT_PATHS: dict[str, str] = {
    "title": "name",
    "description": "description",
    "start_datetime": "startDate",
    "end_datetime": "endDate",
    "canonical_url": "url",
    "venue": "location.name",
    "address": "location.address",
    "image": "image",
    "source_category": "eventStatus",
    "external_source_id": "identifier",
    "offers_url": "offers.url",
}

_RAW_FIELDS = tuple(_DEFAULT_PATHS.keys())


def _flatten_node(node: Any) -> list[dict[str, Any]]:
    if isinstance(node, list):
        result: list[dict[str, Any]] = []
        for item in node:
            result.extend(_flatten_node(item))
        return result
    if isinstance(node, dict):
        if isinstance(node.get("@graph"), list):
            return _flatten_node(node["@graph"])
        return [node]
    return []


def _find_jsonld_nodes(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    nodes: list[dict[str, Any]] = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        text = script.string or script.get_text()
        if not text or not text.strip():
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        nodes.extend(_flatten_node(data))
    return nodes


def _is_event_type(node: dict[str, Any]) -> bool:
    type_value = node.get("@type")
    if isinstance(type_value, str):
        return "event" in type_value.lower()
    if isinstance(type_value, list):
        return any("event" in str(t).lower() for t in type_value)
    return False


def _extract_image(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        url = value.get("url")
        return url if isinstance(url, str) else None
    if isinstance(value, list) and value:
        return _extract_image(value[0])
    return None


def _extract_address(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        parts = [
            value.get("streetAddress"),
            value.get("addressLocality"),
            value.get("addressRegion"),
            value.get("postalCode"),
        ]
        joined = ", ".join(str(p) for p in parts if p)
        return joined or None
    return None


class JsonLdEventPattern:
    name = NAME

    def extract(self, response: FetchResponse, config: SiteConfiguration) -> list[EventCandidate]:
        nodes = _find_jsonld_nodes(response.text)
        paths = {
            **_DEFAULT_PATHS,
            **{k: v for k, v in config.json_paths.items() if k in _RAW_FIELDS},
        }

        candidates: list[EventCandidate] = []
        for node in nodes:
            if not _is_event_type(node):
                continue

            raw: dict[str, Any] = {}
            field_source_paths: dict[str, str] = {}
            warnings: list[str] = []

            for field_name, path in paths.items():
                result = resolve_json_path(node, path)
                field_source_paths[field_name] = result.source_path or f"jsonpath:{path}"
                raw[field_name] = result.value

            raw["image"] = _extract_image(raw.get("image"))
            raw["address"] = _extract_address(raw.get("address"))

            canonical_url = raw.get("canonical_url")
            if not canonical_url and config.allow_page_url_as_canonical_fallback:
                canonical_url = response.final_url
                field_source_paths["canonical_url"] = "fallback:page_url"
            if not canonical_url and config.allow_offers_url_as_event_url:
                offers_url = raw.get("offers_url")
                if offers_url:
                    canonical_url = offers_url
                    field_source_paths["canonical_url"] = field_source_paths.get(
                        "offers_url", "jsonpath:offers.url"
                    )
            raw["canonical_url"] = canonical_url

            raw_record_hash = hashlib.sha256(
                json.dumps(node, sort_keys=True, default=str).encode("utf-8")
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
                    warnings=tuple(warnings),
                    raw_record_hash=raw_record_hash,
                )
            )
        return candidates
