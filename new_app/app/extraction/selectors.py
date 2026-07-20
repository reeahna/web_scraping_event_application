"""Field extraction: CSS selectors and a small, hand-rolled JSON path
resolver. No XPath, no arbitrary JavaScript selectors, no code execution —
selectors are plain strings interpreted by BeautifulSoup's own `.select()`
or by the dotted/bracket resolver below.
"""

from __future__ import annotations

import re
from typing import Any

from bs4 import Tag

from app.extraction.types import FieldExtractionResult

_JSON_PATH_SEGMENT_RE = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


class InvalidSelectorError(ValueError):
    pass


def validate_css_selector(selector: str) -> str:
    """Rejects anything that isn't a real CSS selector BeautifulSoup/soupsieve
    can parse — the only defense needed against "arbitrary JavaScript
    selectors or execution," since there is no JS execution path here at all."""
    from bs4 import BeautifulSoup

    selector = selector.strip()
    if not selector:
        raise InvalidSelectorError("Selector is required")
    if len(selector) > 500:
        raise InvalidSelectorError("Selector must be 500 characters or fewer")
    try:
        BeautifulSoup("<html></html>", "html.parser").select(selector)
    except Exception as exc:  # soupsieve raises its own SelectorSyntaxError subclasses
        raise InvalidSelectorError(f"Invalid CSS selector: {exc}") from exc
    return selector


def resolve_css(node: Tag, selector: str, attribute: str | None = None) -> FieldExtractionResult:
    warnings: list[str] = []
    try:
        target = node.select_one(selector)
    except Exception as exc:
        return FieldExtractionResult(
            value=None, source_path=f"css:{selector}", warnings=(str(exc),)
        )

    if target is None:
        return FieldExtractionResult(
            value=None, source_path=f"css:{selector}", warnings=("no matching element",)
        )

    if attribute:
        value = target.get(attribute)
        source_path = f"css:{selector}@{attribute}"
        if value is None:
            warnings.append(f"attribute '{attribute}' not present")
        return FieldExtractionResult(value=value, source_path=source_path, warnings=tuple(warnings))

    text = target.get_text(" ", strip=True)
    return FieldExtractionResult(value=text or None, source_path=f"css:{selector}", warnings=())


def _parse_json_path(path: str) -> list[str | int]:
    segments: list[str | int] = []
    for match in _JSON_PATH_SEGMENT_RE.finditer(path):
        key, index = match.groups()
        if key is not None:
            segments.extend(part for part in key.split(".") if part)
        elif index is not None:
            segments.append(int(index))
    return segments


def resolve_json_path(data: Any, path: str) -> FieldExtractionResult:
    """Supports the small subset actually needed for schema.org JSON-LD and
    WordPress REST payloads: dot-separated keys (location.address.
    addressLocality) and numeric list indices (offers.0.price). No wildcard
    globbing here — JSON-LD's @graph/array-vs-object duality is handled by
    the json_ld pattern itself before this resolver ever runs, since that
    shape is bespoke to schema.org, not a generic JSON path feature."""
    current = data
    for segment in _parse_json_path(path):
        if isinstance(segment, int):
            if not isinstance(current, list) or not (-len(current) <= segment < len(current)):
                return FieldExtractionResult(
                    value=None, source_path=f"jsonpath:{path}", warnings=("index out of range",)
                )
            current = current[segment]
        else:
            if not isinstance(current, dict) or segment not in current:
                return FieldExtractionResult(
                    value=None, source_path=f"jsonpath:{path}", warnings=("key not found",)
                )
            current = current[segment]
    return FieldExtractionResult(value=current, source_path=f"jsonpath:{path}", warnings=())
