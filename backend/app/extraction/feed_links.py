"""Calendar feeds a listing page advertises.

Many calendar platforms render a listing page that no detector can read
directly, while linking a machine-readable feed of the same calendar: an
iCalendar (`.ics`, `webcal:`) subscription or an RSS/Atom feed. This module
finds those links in a page's own markup — `<link rel="alternate">` first, then
anchors whose URL or text names a feed — and ranks them, iCalendar first
because it carries real event dates. It is pure: fetching and detecting the
feed is the caller's job (through the SSRF-safe fetch layer).

Nothing here is site-specific; only the page's own markup is read.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

MAX_FEED_CANDIDATES = 3

_ICS_TYPES = ("text/calendar",)
_XML_FEED_TYPES = ("application/rss+xml", "application/atom+xml")
_ICS_URL_RE = re.compile(
    r"(\.ics|\.ical|\.ifb)(\?|$)|/ical(/|\.|\?|$)|/ics(/|\?|$)|[?&](format|type|output)=i?cal",
    re.I,
)
_XML_URL_RE = re.compile(
    r"(\.rss|\.atom)(\?|$)|/(rss|atom)(/|\.xml|\?|$)|/feed/?(\?|$)"
    r"|[?&](format|type|output)=(rss|atom)",
    re.I,
)
_XML_TEXT_RE = re.compile(r"\b(rss|atom)\b", re.I)
# A link carrying one event's id is an "add this event" download, not the
# calendar's feed.
_SINGLE_EVENT_RE = re.compile(r"[?&](event_?id|eventid|id|ev)=", re.I)


def _absolute(href: str, base_url: str) -> str | None:
    href = href.strip()
    if not href:
        return None
    if href.lower().startswith("webcal://"):
        href = "https://" + href[len("webcal://"):]
    url = urljoin(base_url, href)
    if urlsplit(url).scheme not in ("http", "https"):
        return None
    return url


def feed_links(html: str, base_url: str) -> list[str]:
    """Up to MAX_FEED_CANDIDATES feed URLs advertised by `html`, best first:
    declared iCalendar, iCalendar anchors, declared RSS/Atom, RSS/Atom anchors.
    Deterministic (document order within each rank)."""
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:  # noqa: BLE001 - unparseable markup simply advertises nothing
        return []

    ranked: list[tuple[int, int, str]] = []
    position = 0
    for link in soup.find_all("link", href=True):
        rel = " ".join(link.get("rel") or []).lower()
        kind = (link.get("type") or "").lower().split(";")[0].strip()
        if "alternate" not in rel:
            continue
        url = _absolute(link["href"], base_url)
        if url is None:
            continue
        if kind in _ICS_TYPES:
            ranked.append((0, position, url))
        elif kind in _XML_FEED_TYPES:
            ranked.append((2, position, url))
        position += 1

    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        url = _absolute(href, base_url)
        if url is None or _SINGLE_EVENT_RE.search("?" + urlsplit(url).query):
            continue
        text = anchor.get_text(" ", strip=True)
        is_webcal = href.strip().lower().startswith("webcal:")
        if is_webcal or _ICS_URL_RE.search(url):
            ranked.append((1, position, url))
        elif _XML_URL_RE.search(url) or (
            _XML_TEXT_RE.search(text) and urlsplit(url).path.lower().endswith(".xml")
        ):
            ranked.append((3, position, url))
        position += 1

    seen: set[str] = set()
    result: list[str] = []
    for _, _, url in sorted(ranked):
        if url in seen:
            continue
        seen.add(url)
        result.append(url)
        if len(result) >= MAX_FEED_CANDIDATES:
            break
    return result
