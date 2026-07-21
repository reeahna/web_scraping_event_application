"""Candidate CSS selector generation + stability scoring.

Two rules govern everything here:

1. A generated selector is *positional-free*. There is no `nth-child`, no
   `nth-of-type`, no sibling combinator, and no index anywhere — a selector
   that depends on a card's ordering breaks the moment the site reorders or
   inserts a card.
2. A generated selector is only ever kept if it actually resolves back to
   the element it was generated from, inside that card. That single check is
   what makes "one value per card" a property of generation rather than
   something scoring has to rescue afterwards.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from bs4 import Tag

from app.extraction.inference.policy import AutoOnboardingPolicy
from app.extraction.selectors import InvalidSelectorError, validate_css_selector

# Class/id tokens that carry no meaning for a human reader either — a
# generated hash, a numeric fragment, or a single character. Excluded from
# selectors entirely, because a selector built on one is not stable across a
# site's next asset build.
_NUMERIC_RUN_RE = re.compile(r"\d{3,}")
_HEX_HASH_RE = re.compile(r"^[a-z]*[0-9a-f]{6,}$", re.IGNORECASE)
_ALL_DIGITS_RE = re.compile(r"^\d+$")
_WORD_SPLIT_RE = re.compile(r"[^a-z0-9]+")

# Tags whose identity is itself semantic evidence, so they are worth a
# tag-only selector even with no classes at all.
_SEMANTIC_TAGS: frozenset[str] = frozenset(
    {"h1", "h2", "h3", "h4", "h5", "h6", "time", "img", "address", "picture", "source"}
)

# Attributes worth reading a value out of, per tag. `None` (element text) is
# always tried in addition to these.
_ATTRIBUTE_CANDIDATES: dict[str, tuple[str, ...]] = {
    "a": ("href",),
    "img": ("src", "data-src", "data-lazy-src", "data-original", "srcset"),
    "source": ("srcset", "data-srcset"),
    "time": ("datetime",),
    "meta": ("content",),
    "link": ("href",),
    "object": ("data",),
}

_MAX_CLASSES_IN_SELECTOR = 3


def is_stable_class_token(token: str) -> bool:
    token = token.strip()
    if len(token) < 2 or len(token) > 40:
        return False
    if _ALL_DIGITS_RE.match(token):
        return False
    if token[0].isdigit():
        return False
    if _NUMERIC_RUN_RE.search(token):
        return False
    return not _HEX_HASH_RE.match(token)


def stable_classes(tag: Tag) -> list[str]:
    raw = tag.get("class") or []
    return [c for c in raw if is_stable_class_token(c)][:_MAX_CLASSES_IN_SELECTOR]


def selector_parts(selector: str) -> int:
    """Combinator-separated parts. `h3.title a` is 2; `div.a > div.b c` is 3."""
    return len([p for p in re.split(r"\s*[>+~]\s*|\s+", selector.strip()) if p])


def selector_stability(selector: str, policy: AutoOnboardingPolicy) -> float:
    """1.0 for a single, semantic, one-part selector; decreasing as the chain
    deepens or the string grows. Never negative."""
    parts = selector_parts(selector)
    depth_penalty = 0.25 * max(0, parts - 1)
    length_penalty = min(0.25, len(selector) / (policy.max_selector_length * 4))
    return max(0.0, 1.0 - depth_penalty - length_penalty)


def name_tokens(value: str | None) -> set[str]:
    """Splits a class/id/itemprop/aria-label/data-attribute name into
    lowercase word tokens, so `m-eventItem__title` yields {m, event, item,
    title} rather than one opaque blob."""
    if not value:
        return set()
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", value)
    return {t for t in _WORD_SPLIT_RE.split(spaced.lower()) if len(t) > 1}


@dataclass(frozen=True)
class SelectorCandidate:
    """A generated selector plus every naming signal observed on the element
    it came from — scoring reads its evidence from here rather than from the
    selector string, so `[data-venue]` and `aria-label="Venue"` count the
    same way a `.venue` class does."""

    selector: str
    attribute: str | None
    tag_name: str
    hints: frozenset[str] = field(default_factory=frozenset)
    itemprop: str | None = None
    # Structural evidence that doesn't come from any name: a card's first
    # anchor carrying text is the event's own link on layouts that give it
    # no class at all.
    is_first_anchor: bool = False


def _class_selectors(tag_name: str, classes: list[str]) -> list[str]:
    if not classes:
        return []
    selectors = [tag_name + "".join(f".{c}" for c in classes)]
    for cls in classes:
        selectors.append(f"{tag_name}.{cls}")
        selectors.append(f".{cls}")
    return selectors


def _element_hints(element: Tag) -> frozenset[str]:
    hints: set[str] = set()
    for cls in element.get("class") or []:
        hints |= name_tokens(cls)
    hints |= name_tokens(element.get("id"))
    hints |= name_tokens(element.get("itemprop"))
    hints |= name_tokens(element.get("aria-label"))
    for attr_name in element.attrs:
        if attr_name.startswith("data-"):
            hints |= name_tokens(attr_name[len("data-") :])
    return frozenset(hints)


def _attribute_options(element: Tag) -> list[str | None]:
    options: list[str | None] = [None]
    for attribute in _ATTRIBUTE_CANDIDATES.get(element.name, ()):
        if element.has_attr(attribute):
            options.append(attribute)
    # A CSS background image is a real image source on plenty of card
    # layouts; the value is extracted later by an inferred transformation.
    style = element.get("style")
    if isinstance(style, str) and "url(" in style:
        options.append("style")
    return options


def _raw_selectors(card: Tag, element: Tag) -> list[tuple[str, frozenset[str]]]:
    """(selector, extra naming hints contributed by the selector itself).
    An ancestor-qualified selector inherits its ancestor's naming evidence —
    `h3.event-title a` is evidence about the anchor, not just the heading."""
    selectors: list[tuple[str, frozenset[str]]] = []
    tag_name = element.name
    none: frozenset[str] = frozenset()
    itemprop = element.get("itemprop")
    if isinstance(itemprop, str) and itemprop:
        selectors.append((f'[itemprop="{itemprop}"]', none))
        selectors.append((f'{tag_name}[itemprop="{itemprop}"]', none))

    classes = stable_classes(element)
    selectors.extend((selector, none) for selector in _class_selectors(tag_name, classes))

    for attr_name in element.attrs:
        if attr_name.startswith("data-") and is_stable_class_token(attr_name[len("data-") :]):
            selectors.append((f"{tag_name}[{attr_name}]", none))

    if tag_name in _SEMANTIC_TAGS:
        selectors.append((tag_name, none))

    # Ancestor-qualified form. This is what produces `h3.event-title a` for a
    # classless anchor inside a titled heading — the specific event-title
    # link, rather than the broad `a` that would also match a category chip
    # or a "read more" link.
    for ancestor in element.parents:
        if ancestor is card or not isinstance(ancestor, Tag):
            break
        ancestor_hints = _element_hints(ancestor)
        ancestor_classes = stable_classes(ancestor)
        if ancestor_classes:
            prefix = f"{ancestor.name}.{ancestor_classes[0]}"
            selectors.append((f"{prefix} {tag_name}", ancestor_hints))
            if classes:
                selectors.append((f"{prefix} {tag_name}.{classes[0]}", ancestor_hints))
            break
        if ancestor.name in _SEMANTIC_TAGS:
            selectors.append((f"{ancestor.name} {tag_name}", ancestor_hints))
            break
    return selectors


def _first_anchor(card: Tag) -> Tag | None:
    for anchor in card.find_all("a", href=True):
        if anchor.get_text(strip=True):
            return anchor
    return None


def candidate_selectors(
    card: Tag, element: Tag, policy: AutoOnboardingPolicy
) -> list[SelectorCandidate]:
    """Every positional-free selector that resolves, within `card`, to
    exactly `element` — paired with each readable attribute on it."""
    element_hints = _element_hints(element)
    itemprop = element.get("itemprop")
    itemprop = itemprop if isinstance(itemprop, str) else None
    is_first_anchor = element.name == "a" and _first_anchor(card) is element

    kept: list[SelectorCandidate] = []
    seen: set[str] = set()
    for selector, extra_hints in _raw_selectors(card, element):
        if selector in seen:
            continue
        seen.add(selector)
        hints = element_hints | extra_hints
        if len(selector) > policy.max_selector_length:
            continue
        if selector_parts(selector) > policy.max_selector_parts:
            continue
        try:
            validate_css_selector(selector)
        except InvalidSelectorError:
            continue
        # The generation-time guarantee: this selector picks out this element
        # and nothing else in this card.
        if card.select_one(selector) is not element:
            continue
        for attribute in _attribute_options(element):
            kept.append(
                SelectorCandidate(
                    selector=selector,
                    attribute=attribute,
                    tag_name=element.name,
                    hints=frozenset(hints),
                    itemprop=itemprop,
                    is_first_anchor=is_first_anchor,
                )
            )
    return kept
