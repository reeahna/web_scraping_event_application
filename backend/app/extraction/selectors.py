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

# Class names the common CSS frameworks and CMSs use for text that is read out
# to screen readers but never shown ("Guadalupe Maravilla<span
# class="sr-only">, article</span>"). A title is what a visitor sees, so this
# text is left out of it.
SCREEN_READER_ONLY_CLASSES = frozenset(
    {
        "sr-only",
        "visually-hidden",
        "visuallyhidden",
        "screen-reader-text",
        "screen-reader-only",
        "screenreader-only",
        "element-invisible",
        "show-for-sr",
        "u-visually-hidden",
    }
)


def _is_screen_reader_only(tag: Tag) -> bool:
    classes = tag.get("class") or []
    return any(str(name).lower() in SCREEN_READER_ONLY_CLASSES for name in classes)


def visible_text(target: Tag) -> str:
    """The element's text without its screen-reader-only parts. When the
    element holds nothing else (its whole text is screen-reader-only), the full
    text is returned instead, so a field is never emptied by this."""
    parts: list[str] = []
    for piece in target.strings:
        hidden = False
        for parent in piece.parents:
            if isinstance(parent, Tag) and _is_screen_reader_only(parent):
                hidden = True
                break
            if parent is target:
                break
        if not hidden and piece.strip():
            parts.append(piece.strip())
    text = " ".join(parts)
    return text or target.get_text(" ", strip=True)


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


def resolve_css(
    node: Tag, selector: str, attribute: str | None = None, *, visible_only: bool = False
) -> FieldExtractionResult:
    """`visible_only` drops screen-reader-only text (see visible_text) from an
    element's text; it has no effect on an attribute value."""
    warnings: list[str] = []
    # ":scope" resolves to the card element itself, so a field can be read off
    # the container — e.g. a whole-card `<a href>`'s own href, or an attribute
    # on the card. BeautifulSoup's select_one(":scope") does not match self, so
    # it is handled explicitly here.
    if selector.strip() == ":scope":
        target: Tag | None = node
    else:
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

    text = visible_text(target) if visible_only else target.get_text(" ", strip=True)
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
