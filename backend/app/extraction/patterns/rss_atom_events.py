"""RSS 2.0 / Atom feed extraction pattern.

Parses feed XML with `defusedxml.ElementTree`, so a feed carrying a billion-
laughs entity expansion or an external-entity reference is refused rather than
processed. Handles both RSS `<item>` and Atom `<entry>` by element local name,
so namespace prefixes do not matter.

Deliberate restraint: a feed entry is **not** assumed to be an event, and the
publication date (`pubDate`/`published`) is **not** used as the event date by
default. The event's start comes only from a configured element via
`config.json_paths["start_datetime"]`; without it `start_date` is absent and
the source lands in review, which is the correct outcome for a generic news
feed. Identity is the entry's `guid`/`id`, so a feed whose entries have no
usable link still deduplicates.

Venue: event feeds commonly name the place in an element of their own: the
RSS event module's `ev:location`, NYC Parks' `event:location`, or a plain
`<venue>`/`<location>`. Only that element is read (placeholders such as "TBA"
or "See Event Description" are dropped); a venue is never taken from the
title or description. `json_paths` can name another element for `venue` or
`address`. Coordinates come from a "lat, lon" element (`event:coordinates`,
`georss:point`) or W3C `geo:lat`/`geo:long`, and only when both are in range.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any
from xml.etree.ElementTree import Element

from defusedxml.ElementTree import ParseError, fromstring

from app.extraction.locations import clean_venue
from app.extraction.types import EventCandidate, FetchResponse
from app.schemas.extraction import SiteConfiguration

NAME = "rss_atom_events"
PATTERN_VERSION = "1"

# An "&" that does not start an entity or character reference. Some feeds
# (Carbonhouse sites, e.g. "Broadway & Theatre" in an event type) write it
# bare, which is not well-formed XML.
_BARE_AMPERSAND = re.compile(r"&(?!#\d+;|#x[0-9A-Fa-f]+;|[A-Za-z][A-Za-z0-9]*;)")


def _parse(text: str) -> Element | None:
    """Parse the feed, retrying once with bare ampersands escaped."""
    try:
        return fromstring(text)
    except (ParseError, ValueError):
        pass
    repaired = _BARE_AMPERSAND.sub("&amp;", text)
    if repaired == text:
        return None
    try:
        return fromstring(repaired)
    except (ParseError, ValueError):
        return None


# Our field name -> the feed element local names to read, in order. The event
# date is intentionally absent: it must be configured, never assumed.
_DEFAULT_ELEMENTS: dict[str, tuple[str, ...]] = {
    "title": ("title",),
    "description": ("description", "summary", "content"),
    "external_source_id": ("guid", "id"),
    "source_category": ("category",),
    "venue": ("location", "venue"),
    # No common element; read only when `json_paths["address"]` names one.
    "address": (),
}

# A "lat, lon" (or "lat lon") pair in one element.
_PAIR_ELEMENTS = ("coordinates", "point")
_PAIR_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*[, ]\s*(-?\d+(?:\.\d+)?)\s*$")


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _child_text(item: Element, names: tuple[str, ...]) -> str | None:
    for child in item:
        if _localname(child.tag) in names:
            text = (child.text or "").strip()
            if text:
                return text
    return None


def _coordinate(value: str | None, limit: float) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if -limit <= number <= limit else None


def _coordinates(item: Element) -> tuple[float, float, str] | None:
    """(latitude, longitude, source element) the entry states, or None."""
    pair = _child_text(item, _PAIR_ELEMENTS)
    if pair is not None:
        match = _PAIR_RE.match(pair)
        if match:
            lat, lon = _coordinate(match[1], 90), _coordinate(match[2], 180)
            if lat is not None and lon is not None:
                return lat, lon, "coordinates|point"
        return None
    lat = _coordinate(_child_text(item, ("lat",)), 90)
    lon = _coordinate(_child_text(item, ("long", "lon")), 180)
    if lat is not None and lon is not None:
        return lat, lon, "lat,long"
    return None


def _link(item: Element) -> str | None:
    """RSS link is element text; Atom link is a `<link href=...>` attribute
    (preferring rel=alternate or an unspecified rel)."""
    best: str | None = None
    for child in item:
        if _localname(child.tag) != "link":
            continue
        if child.text and child.text.strip():
            return child.text.strip()
        href = child.get("href")
        if href:
            rel = child.get("rel", "alternate")
            if rel == "alternate":
                return href
            best = best or href
    return best


def _items(root: Element) -> list[Element]:
    items: list[Element] = []
    for element in root.iter():
        if _localname(element.tag) in ("item", "entry"):
            items.append(element)
    return items


class RssAtomEventsPattern:
    name = NAME

    def extract(self, response: FetchResponse, config: SiteConfiguration) -> list[EventCandidate]:
        root = _parse(response.text)
        if root is None:
            return []

        # A configured element supplies the event date; publication date is
        # never substituted.
        start_element = config.json_paths.get("start_datetime")
        end_element = config.json_paths.get("end_datetime")
        overrides = {
            field: (config.json_paths[field],)
            for field in _DEFAULT_ELEMENTS
            if field in config.json_paths
        }
        elements = {**_DEFAULT_ELEMENTS, **overrides}

        candidates: list[EventCandidate] = []
        for index, item in enumerate(_items(root)):
            raw: dict[str, Any] = {
                field: _child_text(item, names) for field, names in elements.items()
            }
            raw["canonical_url"] = _link(item)
            raw["start_datetime"] = (
                _child_text(item, (start_element,)) if start_element else None
            )
            raw["end_datetime"] = _child_text(item, (end_element,)) if end_element else None
            raw["venue"] = clean_venue(raw.get("venue"), raw.get("title"))
            coordinates = _coordinates(item)
            if coordinates is not None:
                raw["latitude"], raw["longitude"], coordinate_source = coordinates

            field_source_paths = {
                "title": f"item[{index}].title",
                "canonical_url": f"item[{index}].link",
                "external_source_id": f"item[{index}].guid|id",
            }
            if raw["venue"] is not None:
                field_source_paths["venue"] = f"item[{index}]." + "|".join(elements["venue"])
            if raw.get("address") is not None:
                field_source_paths["address"] = f"item[{index}]." + "|".join(elements["address"])
            if coordinates is not None:
                field_source_paths["latitude"] = f"item[{index}].{coordinate_source}"
                field_source_paths["longitude"] = f"item[{index}].{coordinate_source}"

            raw_record_hash = hashlib.sha256(
                (raw.get("external_source_id") or raw.get("canonical_url") or str(index)).encode(
                    "utf-8"
                )
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
