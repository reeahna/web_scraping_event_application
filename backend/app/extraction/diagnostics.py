"""What a run actually received when it read zero events.

A source that answers 200 but yields no events looks the same in the run list
whether the calendar is empty, the site served a bot page, or the layout
changed. This module describes the response in a way a person can compare with
what a browser sees: status, final URL and redirects, type and size, a verdict
(blocked, empty, a different page, or the listing present but not read), and a
structural outline of the page.

It is read-only and records structure only. It never stores page text other
than the title, never stores attribute values other than class and id names,
never stores JSON values (only keys and types), keeps only an allowlist of
response headers (never cookies or authorization), and drops query-string
values and user info from every URL. It must never raise: a diagnostic failure
is recorded as such and the run carries on.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag

from app.extraction.challenge import find_marker, visible_text
from app.extraction.types import FetchResponse
from app.schemas.extraction import SiteConfiguration

logger = logging.getLogger(__name__)

# Response headers worth comparing with a browser's. Anything else (cookies,
# authorization, tokens, tracking ids) is never recorded.
SAFE_HEADERS = (
    "content-type",
    "content-length",
    "content-encoding",
    "content-language",
    "server",
    "cache-control",
    "age",
    "vary",
    "x-cache",
    "cf-cache-status",
    "x-device-type",
    "retry-after",
)
BLOCKING_STATUSES = frozenset({401, 403, 407, 429, 451, 503})
CHALLENGE_MARKERS = ("cloudflare", "access denied", "are you a robot", "captcha")
JS_APP_MARKERS = ("__NEXT_DATA__", 'id="__nuxt"', "ng-version", "data-reactroot")

MAX_SKELETON_LINES = 40
MAX_SKELETON_DEPTH = 4
MAX_CLASSES = 15
MAX_JSON_KEYS = 20
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,60}$")
_SPACE_RE = re.compile(r"\s+")


def sanitize_url(url: str | None) -> str | None:
    """`url` without user info or fragment, and with every query value
    replaced, so a token in a query string is never recorded."""
    if not url:
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        return "[unparseable url]"
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    query = "&".join(f"{key}=..." for key, _ in parse_qsl(parts.query, keep_blank_values=True))
    return urlunsplit((parts.scheme, host, parts.path, query, ""))


def safe_headers(headers: dict[str, str]) -> dict[str, str]:
    lowered = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    return {name: lowered[name][:120] for name in SAFE_HEADERS if name in lowered}


def _safe_name(value: str) -> str | None:
    return value if _NAME_RE.match(value) else None


def _node_label(tag: Tag) -> str:
    label = tag.name
    tag_id = tag.get("id")
    if isinstance(tag_id, str) and _safe_name(tag_id):
        label += f"#{tag_id}"
    classes = [c for c in (tag.get("class") or []) if _safe_name(c)][:3]
    if classes:
        label += "." + ".".join(classes)
    return label


def _skeleton(root: Tag) -> list[str]:
    """An indented outline of element names, ids and classes, with no text."""
    lines: list[str] = []

    def walk(tag: Tag, depth: int) -> None:
        if len(lines) >= MAX_SKELETON_LINES:
            return
        children = [c for c in tag.children if isinstance(c, Tag)]
        if tag.name in ("script", "style", "noscript", "svg"):
            children = []
        suffix = f" ({len(children)} children)" if children else ""
        lines.append("  " * depth + _node_label(tag) + suffix)
        if depth >= MAX_SKELETON_DEPTH:
            return
        # Runs of identical siblings (a list of cards) are shown once.
        seen: Counter[str] = Counter()
        for child in children:
            label = _node_label(child)
            seen[label] += 1
            if seen[label] == 2:
                lines.append("  " * (depth + 1) + f"... more {label}")
            if seen[label] == 1:
                walk(child, depth + 1)

    walk(root, 0)
    return lines[:MAX_SKELETON_LINES]


def _selectors(config: SiteConfiguration | None) -> dict[str, str]:
    if config is None:
        return {}
    found: dict[str, str] = {}
    if config.event_container_selector:
        found["event_container_selector"] = config.event_container_selector
    for field, rule in (config.field_selectors or {}).items():
        if rule.kind in ("css", "attribute"):
            found[f"field:{field}"] = rule.selector
    return found


def _html_summary(text: str, config: SiteConfiguration | None) -> dict[str, Any]:
    soup = BeautifulSoup(text, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    elements = soup.find_all(True)
    classes: Counter[str] = Counter()
    for tag in elements:
        for name in tag.get("class") or []:
            if _safe_name(name):
                classes[name] += 1
    matches: dict[str, int | str] = {}
    for key, selector in _selectors(config).items():
        try:
            matches[key] = len(soup.select(selector))
        except Exception:  # noqa: BLE001 - an invalid selector is itself a finding
            matches[key] = "invalid selector"
    body = soup.body or soup
    return {
        "title": _SPACE_RE.sub(" ", title)[:160],
        "visible_text_chars": len(visible_text(text)),
        "element_count": len(elements),
        "counts": {
            name: len(soup.find_all(name))
            for name in ("a", "article", "li", "table", "form", "iframe", "script", "template")
        },
        "selector_matches": matches,
        "common_classes": [f"{name} x{count}" for name, count in classes.most_common(MAX_CLASSES)],
        "skeleton": _skeleton(body) if isinstance(body, Tag) else [],
        "challenge_marker": find_marker(text[:20000], CHALLENGE_MARKERS),
        "js_app_markers": [m for m in JS_APP_MARKERS if m in text],
    }


def _json_shape(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        keys = [k for k in value if isinstance(k, str) and _safe_name(k)][:MAX_JSON_KEYS]
        return {
            "type": "object",
            "key_count": len(value),
            "keys": {k: type(value[k]).__name__ for k in keys},
        }
    if isinstance(value, list):
        shape: dict[str, Any] = {"type": "array", "length": len(value)}
        if value:
            shape["first_item"] = _json_shape(value[0]) if isinstance(
                value[0], dict | list
            ) else type(value[0]).__name__
        return shape
    return {"type": type(value).__name__}


def _verdict(response: FetchResponse, summary: dict[str, Any], size: int) -> str:
    html = summary.get("html") or {}
    json_shape = summary.get("json") or {}
    if (
        response.blocked_reason
        or response.status_code in BLOCKING_STATUSES
        or html.get("challenge_marker")
    ):
        return "blocked"
    if response.status_code >= 400:
        return "error_status"
    if size == 0:
        return "empty"
    if json_shape and (json_shape.get("length") == 0 or json_shape.get("key_count") == 0):
        return "empty"
    if html:
        if html["visible_text_chars"] < 200 and html["element_count"] < 30:
            return "empty"
        container = html["selector_matches"].get("event_container_selector")
        if container == 0:
            return "different_page"
        if isinstance(container, int) and container > 0:
            return "listing_found_but_not_read"
    if "ics" in summary and summary["ics"]["vevent_count"] == 0:
        return "empty"
    return "unclear"


def describe_zero_event_response(
    response: FetchResponse, config: SiteConfiguration | None
) -> dict[str, Any]:
    """A sanitized description of `response`, for a run that read no events."""
    try:
        size = len(response.body or b"")
        content_type = (response.content_type or "").lower()
        summary: dict[str, Any] = {
            "status_code": response.status_code,
            "request_url": sanitize_url(response.request_url),
            "final_url": sanitize_url(response.final_url),
            "redirected": bool(response.redirect_history)
            or sanitize_url(response.request_url) != sanitize_url(response.final_url),
            "redirects": [sanitize_url(u) for u in response.redirect_history][:10],
            "content_type": response.content_type,
            "size_bytes": size,
            "truncated": response.truncated,
            "headers": safe_headers(response.headers),
            "blocked_reason": response.blocked_reason,
        }
        text = response.text if size else ""
        stripped = text.lstrip()
        if "json" in content_type or stripped[:1] in ("{", "["):
            try:
                summary["json"] = _json_shape(json.loads(text))
            except ValueError:
                summary["json_error"] = "body is not valid JSON"
        if "calendar" in content_type or stripped.startswith("BEGIN:VCALENDAR"):
            summary["ics"] = {"vevent_count": text.count("BEGIN:VEVENT")}
        if "html" in content_type or stripped[:1] == "<":
            summary["html"] = _html_summary(text, config)
        summary["verdict"] = _verdict(response, summary, size)
        return summary
    except Exception as exc:  # noqa: BLE001 - diagnostics must never fail a run
        return {"error": f"diagnostics failed: {type(exc).__name__}"}


def log_zero_event_diagnostics(website_id: int, run_type: str, summary: dict[str, Any]) -> None:
    """One log line, so a scheduled run's finding is visible in the service
    logs without opening the run."""
    html = summary.get("html") or {}
    logger.info(
        "zero events for website %s (%s run): verdict=%s status=%s size=%s type=%s "
        "final_url=%s title=%r selector_matches=%s",
        website_id,
        run_type,
        summary.get("verdict"),
        summary.get("status_code"),
        summary.get("size_bytes"),
        summary.get("content_type"),
        summary.get("final_url"),
        html.get("title"),
        html.get("selector_matches"),
    )
