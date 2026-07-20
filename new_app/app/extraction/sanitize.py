"""Description/HTML sanitization for extracted content.

No sanitization library exists in this codebase yet (confirmed: no bleach/
nh3/html5lib). BeautifulSoup — already a dependency for HTML parsing — is
sufficient for this scope: strip dangerous tags/attributes/URLs, keep a
small safe-tag allowlist, otherwise fall back to plain text.
"""

from __future__ import annotations

from bs4 import BeautifulSoup, Tag

_DANGEROUS_TAGS = frozenset(
    {"script", "style", "iframe", "frame", "frameset", "form", "object", "embed"}
)
_SAFE_TAGS = frozenset({"p", "br", "a", "strong", "em", "b", "i", "ul", "ol", "li"})
_UNSAFE_URL_SCHEMES = ("javascript:", "data:", "vbscript:")


def _looks_like_html(text: str) -> bool:
    return "<" in text and ">" in text


def sanitize_description(value: str | None) -> str | None:
    """Removes scripts, style elements, event-handler attributes, unsafe
    URL schemes, frames, and forms. Keeps a small safe-tag allowlist;
    anything else collapses to plain text via get_text()."""
    if value is None:
        return None
    if not _looks_like_html(value):
        return value.strip() or None

    soup = BeautifulSoup(value, "html.parser")
    for tag in soup.find_all(_DANGEROUS_TAGS):
        tag.decompose()

    for tag in soup.find_all(True):
        if not isinstance(tag, Tag):
            continue
        for attr in list(tag.attrs):
            if attr.lower().startswith("on"):
                del tag.attrs[attr]
            elif attr.lower() in ("href", "src"):
                value_attr = str(tag.attrs.get(attr, ""))
                if value_attr.strip().lower().startswith(_UNSAFE_URL_SCHEMES):
                    del tag.attrs[attr]
        if tag.name not in _SAFE_TAGS:
            tag.unwrap()

    cleaned = str(soup).strip()
    if not cleaned:
        return None
    # If unwrapping removed every tag, fall back to plain text for a
    # cleaner, more predictable stored value than leftover bare text nodes.
    if not any(isinstance(node, Tag) for node in soup.descendants):
        return soup.get_text(" ", strip=True) or None
    return cleaned


def strip_to_text(value: str | None) -> str | None:
    if value is None:
        return None
    if not _looks_like_html(value):
        return value.strip() or None
    text = BeautifulSoup(value, "html.parser").get_text(" ", strip=True)
    return text or None


__all__ = ["sanitize_description", "strip_to_text"]
