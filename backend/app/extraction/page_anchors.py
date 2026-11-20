"""Page anchors for JSON-LD events that carry no link of their own.

Some sites describe every event in JSON-LD but give their own listings no
`url`, while the same page shows each event as an element with a stable `id`
("<li id="ev20261121ceb17a">"). Without a link such an event cannot be saved,
so a real event was rejected only because its JSON-LD left the field out.

This finds that element and the event's own address on the page:

* An event is matched to the element whose text is exactly the event's name
  (the listing's title), and from there to the nearest enclosing element that
  has an `id`. That element must hold one listing title only, so a day or
  section wrapper around several events is never used.
* Several events with the same name (a market held Saturday and Sunday) are
  told apart by start time when the element states one (an epoch timestamp in
  a `data-*` attribute, or a `<time datetime>`), otherwise by page order, and
  only when the counts agree. An element is never given to two events.
* The link is the site's own permalink for the element when the element
  contains one (a share, report or calendar link on the same site that names
  the element's id), otherwise the page address with `#id`.

Nothing is guessed: an event whose name is not found, or whose match is
ambiguous, gets no anchor and is handled exactly as before.
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

_SKIP_TAGS = frozenset({"script", "style", "template", "noscript", "head", "title"})
_MAX_TITLE_LENGTH = 300
_URL_RE = re.compile(r"https?://[^\s\"'<>]+")
_EPOCH_RE = re.compile(r"^\d{10}(\d{3})?$")


@dataclass(frozen=True)
class PageAnchor:
    element_id: str
    url: str
    # True when `url` is the site's own permalink (the id is in its path or
    # query); False for the `#id` fallback, which normalizes to the page URL.
    is_permalink: bool


@dataclass(frozen=True)
class AnchorRequest:
    key: int
    name: str
    start: str | None


def _norm(text: str | None) -> str:
    return " ".join(html.unescape(text or "").split()).casefold()


def _short_text(tag: Tag) -> str | None:
    """The tag's text, or None once it passes the length of a title (so a
    page-sized container is never fully read)."""
    parts: list[str] = []
    size = 0
    for piece in tag.strings:
        parts.append(piece)
        size += len(piece)
        if size > _MAX_TITLE_LENGTH * 4:
            return None
    text = " ".join(" ".join(parts).split())
    return text if len(text) <= _MAX_TITLE_LENGTH else None


def _title_nodes(soup: BeautifulSoup, names: set[str]) -> dict[str, list[Tag]]:
    """Innermost elements whose whole text is one of `names`, by name, in
    page order."""
    found: dict[str, list[Tag]] = {}
    for tag in soup.find_all(True):
        if tag.name in _SKIP_TAGS or any(p.name in _SKIP_TAGS for p in tag.parents):
            continue
        text = _short_text(tag)
        if text is None:
            continue
        key = _norm(text)
        if key not in names:
            continue
        # Innermost only: drop an ancestor already recorded for this name.
        nodes = found.setdefault(key, [])
        nodes[:] = [n for n in nodes if n not in tag.parents]
        if not any(tag in n.parents for n in nodes):
            nodes.append(tag)
    return found


def _item_for(node: Tag) -> Tag | None:
    current: Tag | None = node
    while current is not None and isinstance(current, Tag):
        element_id = current.get("id")
        if isinstance(element_id, str) and element_id.strip():
            return current
        current = current.parent
    return None


def _instants(item: Tag) -> set[int]:
    """Start instants the element states, as epoch seconds."""
    values: set[int] = set()
    for name, value in item.attrs.items():
        if isinstance(value, str) and name.startswith("data-") and _EPOCH_RE.match(value):
            number = int(value)
            values.add(number // 1000 if len(value) == 13 else number)
    for stamp in item.find_all("time"):
        moment = _parse_instant(stamp.get("datetime"))
        if moment is not None:
            values.add(moment)
    return values


def _parse_instant(value: object) -> int | None:
    if not isinstance(value, str) or "T" not in value:
        return None
    try:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return int(moment.timestamp())


def _strings_in(item: Tag) -> Iterable[str]:
    for tag in [item, *item.find_all(True)]:
        for value in tag.attrs.values():
            if isinstance(value, str):
                yield value


def _permalink(item: Tag, element_id: str, page_url: str) -> str | None:
    """A link on the page's own site, inside the element, that names the
    element's id in its path or query. Links are also read out of mailto and
    calendar URLs, where sites often put the event's own address."""
    host = (urlsplit(page_url).hostname or "").casefold()
    for value in _strings_in(item):
        texts = [value]
        if value.startswith(("mailto:", "http")):
            texts += [unquote(v) for _, v in parse_qsl(urlsplit(value).query)]
        for text in texts:
            for raw in _URL_RE.findall(text):
                url = urljoin(page_url, raw.rstrip(".,;)"))
                parts = urlsplit(url)
                if (parts.hostname or "").casefold() != host:
                    continue
                if element_id in parts.path or element_id in parts.query:
                    return url
    return None


def _anchor(item: Tag, page_url: str) -> PageAnchor:
    element_id = str(item.get("id")).strip()
    permalink = _permalink(item, element_id, page_url)
    if permalink:
        return PageAnchor(element_id=element_id, url=permalink, is_permalink=True)
    base = page_url.split("#", 1)[0]
    return PageAnchor(element_id=element_id, url=f"{base}#{element_id}", is_permalink=False)


def find_page_anchors(
    soup: BeautifulSoup, page_url: str, requests: list[AnchorRequest]
) -> dict[int, PageAnchor]:
    """Anchors for the requested events, keyed by `AnchorRequest.key`."""
    names = {_norm(r.name) for r in requests if _norm(r.name)}
    if not names:
        return {}
    nodes_by_name = _title_nodes(soup, names)
    all_nodes = [node for nodes in nodes_by_name.values() for node in nodes]

    def single_title(item: Tag) -> bool:
        return sum(1 for node in all_nodes if node is item or item in node.parents) == 1

    used: set[int] = set()
    anchors: dict[int, PageAnchor] = {}
    by_name: dict[str, list[AnchorRequest]] = {}
    for request in requests:
        by_name.setdefault(_norm(request.name), []).append(request)

    for name, group in by_name.items():
        items: list[Tag] = []
        for node in nodes_by_name.get(name, []):
            item = _item_for(node)
            if item is not None and single_title(item) and all(item is not i for i in items):
                items.append(item)
        if not items:
            continue

        pairs: dict[int, Tag] = {}
        # By start time where the page states one.
        for request in group:
            moment = _parse_instant(request.start)
            if moment is None:
                continue
            matches = [item for item in items if moment in _instants(item)]
            if len(matches) == 1:
                pairs[request.key] = matches[0]
        # Otherwise (no stated times at all) by page order, and only when
        # every event and element pair up.
        if not pairs and len(items) == len(group):
            pairs = {request.key: item for request, item in zip(group, items, strict=True)}
        for key, item in pairs.items():
            if id(item) in used or list(pairs.values()).count(item) > 1:
                continue
            used.add(id(item))
            anchors[key] = _anchor(item, page_url)
    return anchors
