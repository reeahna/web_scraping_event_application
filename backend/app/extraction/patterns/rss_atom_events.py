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
"""

from __future__ import annotations

import hashlib
from typing import Any
from xml.etree.ElementTree import Element

from defusedxml.ElementTree import ParseError, fromstring

from app.extraction.types import EventCandidate, FetchResponse
from app.schemas.extraction import SiteConfiguration

NAME = "rss_atom_events"
PATTERN_VERSION = "1"

# Our field name -> the feed element local names to read, in order. The event
# date is intentionally absent: it must be configured, never assumed.
_DEFAULT_ELEMENTS: dict[str, tuple[str, ...]] = {
    "title": ("title",),
    "description": ("description", "summary", "content"),
    "external_source_id": ("guid", "id"),
    "source_category": ("category",),
}


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _child_text(item: Element, names: tuple[str, ...]) -> str | None:
    for child in item:
        if _localname(child.tag) in names:
            text = (child.text or "").strip()
            if text:
                return text
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
        try:
            root = fromstring(response.text)
        except (ParseError, ValueError):
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

            field_source_paths = {
                "title": f"item[{index}].title",
                "canonical_url": f"item[{index}].link",
                "external_source_id": f"item[{index}].guid|id",
            }

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
