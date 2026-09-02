"""Pattern detection. Never fetches beyond what's necessary (exactly the one
FetchResponse it's given), never saves events. Each detector inspects the
response and returns a PatternDetectionResult; run_detection() picks a
winner using a fixed reliability order as the only tiebreak — never a
domain-name conditional.
"""

from __future__ import annotations

import json
import re
import warnings as _warnings
from typing import Protocol
from urllib.parse import urljoin

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

from app.extraction.types import FetchResponse, PatternDetectionResult

# Every detector probes every response, so the HTML detectors necessarily run
# BeautifulSoup's HTML parser over XML feeds (RSS/Atom/ICS). That is expected
# and harmless — the feed patterns have their own XML/ICS parsers — so silence
# the "you parsed XML as HTML" advisory rather than let it flood the logs.
_warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

DETECTOR_VERSION = "1"
MIN_PATTERN_CONFIDENCE = 0.6

# Most-specific-structured-pattern first. Used only to break ties when
# multiple detectors clear the confidence threshold on the same response.
# the_events_calendar sits ahead of wordpress_rest: any site running the
# plugin is also plain WordPress, and the more specific pattern should win
# a same-confidence tie.
RELIABILITY_ORDER: tuple[str, ...] = (
    "the_events_calendar",
    "livewhale_json",
    # Simpleview's event API has a highly distinctive nested docs.docs shape,
    # so it rarely contends; placed with the other specific structured APIs.
    "simpleview_events",
    "wordpress_rest",
    "json_ld_event",
    # JSON-in-script patterns sit above generic HTML (structured data beats
    # scraping markup) but below json_ld_event, which owns schema.org.
    "next_data",
    "nuxt_payload",
    "embedded_json",
    # Feed/API patterns keyed on a distinctive response shape, so they rarely
    # contend with the HTML detectors; all sit above generic_html_cards.
    "ics_calendar",
    "rss_atom_events",
    "algolia_search",
    # Event data embedded as a plain JS variable (window.x = [...]). Parsed,
    # never executed. Last structured resort before HTML-card scraping.
    "inline_json_events",
    "generic_html_cards",
)

# A minimum event-like rate a JSON-in-script detector needs to claim a match,
# so a page that merely happens to embed some JSON is not mistaken for an
# event source.
_JSON_EVENTS_MIN_CONFIDENCE = 0.7

_CHALLENGE_MARKERS = ("cloudflare", "access denied", "are you a robot", "captcha")
_JS_FRAMEWORK_MARKERS = ("__NEXT_DATA__", 'id="__nuxt"', "ng-version", "data-reactroot")
_MONTH_NAME = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*"
# The last two alternations cover cards that render the day and month as
# separate elements joined by a separator ("24 / July", "July - 24"), which
# BeautifulSoup's text extraction presents with the separator intact. Without
# them a listing whose date is split across nested elements reads as "no
# dates on this page" and is misclassified as unsupported.
DATE_LIKE_RE = re.compile(
    rf"\b{_MONTH_NAME}\.?\s+\d{{1,2}}\b"
    r"|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b"
    r"|\b\d{4}-\d{2}-\d{2}\b"
    rf"|\b\d{{1,2}}\s*[/|·.-]\s*{_MONTH_NAME}\b"
    rf"|\b{_MONTH_NAME}\s*[/|·.-]\s*\d{{1,2}}\b"
    # Day-first with only whitespace between ("03 September"), the reverse of
    # the leading "Month DD" form — common on European-style event cards.
    rf"|\b\d{{1,2}}\s+{_MONTH_NAME}\b",
    re.IGNORECASE,
)
_WP_GENERATOR_RE = re.compile(r"wordpress\s*[\d.]*", re.IGNORECASE)
_TRIBE_ASSET_RE = re.compile(r"the-events-calendar|tribe-events|tribe-common", re.IGNORECASE)
_TRIBE_CLASS_SELECTOR = "[class*=tribe-events], [class*=tribe-common]"
_TRIBE_REST_ROUTE_RE = re.compile(r"tribe/events/v\d+", re.IGNORECASE)
_TRIBE_REST_URL_RE = re.compile(r'https?://[^\s"\'<>]*tribe/events/v\d+[^\s"\'<>]*', re.IGNORECASE)
_LIVEWHALE_GENERATOR_RE = re.compile(r"livewhale", re.IGNORECASE)
_LIVEWHALE_ASSET_RE = re.compile(r"livewhale|lwcms|lw[-_]calendar", re.IGNORECASE)
_LIVEWHALE_API_ROUTE_RE = re.compile(r"calendar/api/\d+/events|lwapi", re.IGNORECASE)
_LIVEWHALE_API_URL_RE = re.compile(
    r'https?://[^\s"\'<>]*(?:calendar/api/\d+/events|lwapi)[^\s"\'<>]*', re.IGNORECASE
)


class PatternDetector(Protocol):
    def detect(self, response: FetchResponse) -> PatternDetectionResult: ...


def _access_denied_detected(response: FetchResponse) -> bool:
    if response.blocked_reason is not None:
        return True
    lowered = response.text[:5000].lower()
    return any(marker in lowered for marker in _CHALLENGE_MARKERS)


def _browser_required_evidence(soup: BeautifulSoup, response: FetchResponse) -> bool:
    if any(marker in response.text for marker in _JS_FRAMEWORK_MARKERS):
        return True
    body_text = soup.get_text(strip=True)
    script_bytes = sum(len(s.get_text()) for s in soup.find_all("script"))
    # A page with almost no static text but a substantial <script> payload is
    # a reasonably reliable "needs JS to render" signal without guessing further.
    return len(body_text) < 200 and script_bytes > 2000


def _blocked_result(reason: str) -> PatternDetectionResult:
    return PatternDetectionResult(
        pattern_name=None,
        confidence=0.0,
        evidence={"blocked": True},
        discovered_endpoints=(),
        browser_required=False,
        warnings=(reason,),
        detector_version=DETECTOR_VERSION,
        needs_review=True,
    )


def _flatten_jsonld(node: object) -> list[dict]:
    """Expand a JSON-LD node into the concrete objects to type-check: recurse
    through lists, `@graph`, and schema.org `ItemList`/`itemListElement` (each
    element's `item`, or a bare element). Mirrors the extractor's own flatten
    (app.extraction.patterns.jsonld) so detection and extraction agree on what
    an ItemList of Events contains."""
    if isinstance(node, list):
        out: list[dict] = []
        for item in node:
            out.extend(_flatten_jsonld(item))
        return out
    if isinstance(node, dict):
        if isinstance(node.get("@graph"), list):
            return _flatten_jsonld(node["@graph"])
        elements = node.get("itemListElement")
        if isinstance(elements, list):
            out = []
            for element in elements:
                if isinstance(element, dict) and "item" in element:
                    out.extend(_flatten_jsonld(element["item"]))
                else:
                    out.extend(_flatten_jsonld(element))
            return out
        return [node]
    return []


class JsonLdDetector:
    def detect(self, response: FetchResponse) -> PatternDetectionResult:
        if _access_denied_detected(response):
            return _blocked_result("access denied or challenge page detected")

        soup = BeautifulSoup(response.text, "html.parser")
        scripts = soup.find_all("script", attrs={"type": "application/ld+json"})
        event_blocks = 0
        malformed = 0
        for script in scripts:
            text = script.string or script.get_text()
            if not text or not text.strip():
                continue
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                malformed += 1
                continue
            for candidate_node in _flatten_jsonld(data):
                type_value = candidate_node.get("@type")
                types = type_value if isinstance(type_value, list) else [type_value]
                if any(isinstance(t, str) and "event" in t.lower() for t in types):
                    event_blocks += 1

        if event_blocks == 0:
            return PatternDetectionResult(
                pattern_name=None,
                confidence=0.0,
                evidence={"jsonld_script_count": len(scripts), "malformed_blocks": malformed},
                discovered_endpoints=(),
                browser_required=_browser_required_evidence(soup, response),
                warnings=(),
                detector_version=DETECTOR_VERSION,
                needs_review=True,
            )

        confidence = min(0.95, 0.75 + 0.05 * min(event_blocks, 4))
        return PatternDetectionResult(
            pattern_name="json_ld_event",
            confidence=confidence,
            evidence={"event_blocks_found": event_blocks, "malformed_blocks": malformed},
            discovered_endpoints=(),
            browser_required=False,
            warnings=(),
            detector_version=DETECTOR_VERSION,
            needs_review=confidence < MIN_PATTERN_CONFIDENCE,
        )


class StaticHtmlDetector:
    def detect(self, response: FetchResponse) -> PatternDetectionResult:
        if _access_denied_detected(response):
            return _blocked_result("access denied or challenge page detected")

        soup = BeautifulSoup(response.text, "html.parser")
        groups: dict[tuple[str, tuple[str, ...]], list] = {}
        for tag in soup.find_all(True):
            classes = tag.get("class")
            if not classes:
                continue
            key = (tag.name, tuple(sorted(classes)))
            groups.setdefault(key, []).append(tag)

        best_key: tuple[str, tuple[str, ...]] | None = None
        best_score = 0
        for key, elements in groups.items():
            if len(elements) < 3:
                continue
            with_link = sum(1 for el in elements if el.find("a", href=True))
            with_date = sum(
                1 for el in elements if DATE_LIKE_RE.search(el.get_text(" ")) or el.find("time")
            )
            score = min(len(elements), with_link, with_date)
            if score > best_score:
                best_score = score
                best_key = key

        if best_key is None or best_score < 3:
            return PatternDetectionResult(
                pattern_name=None,
                confidence=0.0,
                evidence={"repeated_groups_found": len(groups)},
                discovered_endpoints=(),
                browser_required=_browser_required_evidence(soup, response),
                warnings=(),
                detector_version=DETECTOR_VERSION,
                needs_review=True,
            )

        tag_name, classes = best_key
        confidence = min(0.85, 0.5 + 0.05 * best_score)
        selector = f"{tag_name}.{'.'.join(classes)}"
        return PatternDetectionResult(
            pattern_name="generic_html_cards",
            confidence=confidence,
            evidence={
                "container_selector_candidate": selector,
                "repeated_count": len(groups[best_key]),
            },
            discovered_endpoints=(),
            browser_required=False,
            warnings=(),
            detector_version=DETECTOR_VERSION,
            needs_review=confidence < MIN_PATTERN_CONFIDENCE,
        )


class WordPressRestDetector:
    def detect(self, response: FetchResponse) -> PatternDetectionResult:
        if _access_denied_detected(response):
            return _blocked_result("access denied or challenge page detected")

        soup = BeautifulSoup(response.text, "html.parser")
        evidence: dict[str, object] = {}
        discovered: list[str] = []
        score = 0.0

        # Generic "this is WordPress" evidence is deliberately weak — the wp/v2
        # REST API exposes blog *posts*, which are rarely a site's events, so on
        # its own this must never outrank a pattern that found actual events
        # (schema.org, a real event card listing). Same principle the Events
        # Calendar detector applies. The wp-json discovery link carries the most
        # weight because it is what makes the REST fetch possible at all.
        generator = soup.find("meta", attrs={"name": "generator"})
        if generator and _WP_GENERATOR_RE.search(str(generator.get("content", ""))):
            evidence["generator_meta"] = generator.get("content")
            score += 0.05

        api_link = soup.find("link", attrs={"rel": "https://api.w.org/"})
        if api_link and api_link.get("href"):
            discovered.append(urljoin(response.final_url, api_link["href"]))
            evidence["wp_json_discovery_link"] = discovered[-1]
            score += 0.5

        link_header = response.headers.get("link", "")
        if "wp-json" in link_header or 'rel="https://api.w.org/"' in link_header:
            score += 0.1
            evidence["link_header_hint"] = True

        script_srcs = [s.get("src", "") for s in soup.find_all("script", src=True)]
        if any("wp-content" in src or "wp-includes" in src for src in script_srcs):
            evidence["wp_script_paths"] = True
            score = min(1.0, score + 0.05)

        if score <= 0:
            return PatternDetectionResult(
                pattern_name=None,
                confidence=0.0,
                evidence=evidence,
                discovered_endpoints=tuple(discovered),
                browser_required=_browser_required_evidence(soup, response),
                warnings=(),
                detector_version=DETECTOR_VERSION,
                needs_review=True,
            )

        confidence = min(0.9, score)
        return PatternDetectionResult(
            pattern_name="wordpress_rest",
            confidence=confidence,
            evidence=evidence,
            discovered_endpoints=tuple(discovered),
            browser_required=False,
            warnings=(),
            detector_version=DETECTOR_VERSION,
            needs_review=confidence < MIN_PATTERN_CONFIDENCE,
        )


class TheEventsCalendarDetector:
    """WordPress-generic evidence (generator meta, wp-json discovery link)
    is never sufficient on its own — every WordPress site would otherwise
    match. A positive result always requires at least one The Events
    Calendar-specific signal: a script/link path referencing the plugin, a
    `tribe-events`/`tribe-common` CSS class, or an explicit tribe/events
    REST route reference."""

    def detect(self, response: FetchResponse) -> PatternDetectionResult:
        if _access_denied_detected(response):
            return _blocked_result("access denied or challenge page detected")

        soup = BeautifulSoup(response.text, "html.parser")
        evidence: dict[str, object] = {}
        discovered: list[str] = []
        score = 0.0

        generator = soup.find("meta", attrs={"name": "generator"})
        if generator and _WP_GENERATOR_RE.search(str(generator.get("content", ""))):
            evidence["generator_meta"] = generator.get("content")
            score += 0.05

        wp_json_root: str | None = None
        api_link = soup.find("link", attrs={"rel": "https://api.w.org/"})
        if api_link and api_link.get("href"):
            wp_json_root = urljoin(response.final_url, api_link["href"])
            evidence["wp_json_discovery_link"] = wp_json_root
            score += 0.1

        asset_urls = [s.get("src", "") for s in soup.find_all("script", src=True)]
        asset_urls += [link.get("href", "") for link in soup.find_all("link", href=True)]
        tribe_assets = [url for url in asset_urls if _TRIBE_ASSET_RE.search(url)]
        if tribe_assets:
            evidence["tribe_asset_references"] = tribe_assets[:5]
            score += 0.5

        tribe_class_hits = soup.select(_TRIBE_CLASS_SELECTOR)
        if tribe_class_hits:
            evidence["tribe_css_class_count"] = len(tribe_class_hits)
            score += 0.2

        route_reference = False
        for link in soup.find_all("link", href=True):
            if _TRIBE_REST_ROUTE_RE.search(link["href"]):
                route_url = urljoin(response.final_url, link["href"])
                discovered.append(route_url)
                evidence["tribe_rest_route_link"] = route_url
                route_reference = True
                score += 0.4
                break
        if not route_reference:
            for script in soup.find_all("script"):
                text = script.string or script.get_text() or ""
                url_match = _TRIBE_REST_URL_RE.search(text)
                if url_match:
                    discovered.append(url_match.group(0))
                    evidence["tribe_rest_route_in_script"] = url_match.group(0)
                    route_reference = True
                    score += 0.4
                    break
                if _TRIBE_REST_ROUTE_RE.search(text):
                    evidence["tribe_rest_route_in_script"] = True
                    route_reference = True
                    score += 0.3
                    break

        has_tribe_evidence = bool(tribe_assets) or bool(tribe_class_hits) or route_reference
        if not has_tribe_evidence:
            # Generic WordPress evidence alone (generator meta, wp-json
            # discovery link) never classifies a site as The Events
            # Calendar — that would misclassify every WordPress site.
            return PatternDetectionResult(
                pattern_name=None,
                confidence=0.0,
                evidence=evidence,
                discovered_endpoints=tuple(discovered),
                browser_required=_browser_required_evidence(soup, response),
                warnings=(),
                detector_version=DETECTOR_VERSION,
                needs_review=True,
            )

        if not discovered and wp_json_root:
            # Derive the conventional REST endpoint from the discovered
            # wp-json root — a deterministic route convention, not a guess
            # about which site this is.
            derived = urljoin(wp_json_root, "tribe/events/v1/events")
            discovered.append(derived)
            evidence["derived_endpoint"] = derived

        confidence = min(0.95, score)
        return PatternDetectionResult(
            pattern_name="the_events_calendar",
            confidence=confidence,
            evidence=evidence,
            discovered_endpoints=tuple(discovered),
            browser_required=False,
            warnings=(),
            detector_version=DETECTOR_VERSION,
            needs_review=confidence < MIN_PATTERN_CONFIDENCE,
        )


def _looks_like_livewhale_event(node: object) -> bool:
    if not isinstance(node, dict):
        return False
    has_identity = "id" in node or "occur_id" in node
    return has_identity and "date_ts" in node


class LiveWhaleDetector:
    """LiveWhale is an unrelated CMS from WordPress/Tribe, so there's no
    generic-evidence overlap to guard against here the way there is between
    the_events_calendar and wordpress_rest — but the same principle holds: a
    positive result always requires a LiveWhale-specific signal (an asset
    path referencing the CMS, a discovered calendar API route, or the
    response itself already being a LiveWhale-shaped JSON payload). Generic
    generator-meta text alone only ever adds confidence on top of one of
    those, never triggers a match by itself. Never classified from the
    page's own URL string."""

    def detect(self, response: FetchResponse) -> PatternDetectionResult:
        if _access_denied_detected(response):
            return _blocked_result("access denied or challenge page detected")

        # Direct evidence: the response body IS a LiveWhale-shaped JSON API
        # response (e.g. listing_url was pointed straight at the API).
        try:
            payload = json.loads(response.text)
        except (json.JSONDecodeError, ValueError):
            payload = None
        if payload is not None:
            events: list | None = None
            if isinstance(payload, dict) and isinstance(payload.get("events"), list):
                events = payload["events"]
            elif isinstance(payload, list):
                events = payload
            if events and all(_looks_like_livewhale_event(e) for e in events[:5]):
                return PatternDetectionResult(
                    pattern_name="livewhale_json",
                    confidence=0.9,
                    evidence={"json_shape_match": True, "sample_event_count": len(events)},
                    discovered_endpoints=(response.final_url,),
                    browser_required=False,
                    warnings=(),
                    detector_version=DETECTOR_VERSION,
                    needs_review=False,
                )

        soup = BeautifulSoup(response.text, "html.parser")
        evidence: dict[str, object] = {}
        discovered: list[str] = []
        score = 0.0

        generator = soup.find("meta", attrs={"name": "generator"})
        if generator and _LIVEWHALE_GENERATOR_RE.search(str(generator.get("content", ""))):
            evidence["generator_meta"] = generator.get("content")
            score += 0.15

        asset_urls = [s.get("src", "") for s in soup.find_all("script", src=True)]
        asset_urls += [link.get("href", "") for link in soup.find_all("link", href=True)]
        livewhale_assets = [url for url in asset_urls if _LIVEWHALE_ASSET_RE.search(url)]
        if livewhale_assets:
            evidence["livewhale_asset_references"] = livewhale_assets[:5]
            score += 0.5

        route_reference = False
        for link in soup.find_all("link", href=True):
            if _LIVEWHALE_API_ROUTE_RE.search(link["href"]):
                route_url = urljoin(response.final_url, link["href"])
                discovered.append(route_url)
                evidence["livewhale_api_route_link"] = route_url
                route_reference = True
                score += 0.4
                break
        if not route_reference:
            for script in soup.find_all("script"):
                text = script.string or script.get_text() or ""
                url_match = _LIVEWHALE_API_URL_RE.search(text)
                if url_match:
                    discovered.append(url_match.group(0))
                    evidence["livewhale_api_route_in_script"] = url_match.group(0)
                    route_reference = True
                    score += 0.4
                    break
                if _LIVEWHALE_API_ROUTE_RE.search(text):
                    evidence["livewhale_api_route_in_script"] = True
                    route_reference = True
                    score += 0.3
                    break

        # Modern LiveWhale installs expose events at the deterministic
        # `/live/json/events` feed rather than the older `calendar/api/N/events`
        # route, and don't link it from the page. When LiveWhale is present
        # (asset evidence) but no explicit route was discovered, derive that
        # conventional feed from the site origin so onboarding has an endpoint
        # to configure — the same "known CMS route" principle as WordPress's
        # wp/v2/posts. Deterministic convention, never inferred from the host.
        if livewhale_assets and not discovered:
            derived = urljoin(response.final_url, "/live/json/events")
            discovered.append(derived)
            evidence["livewhale_derived_json_feed"] = derived

        # Generic generator-meta text is deliberately excluded from this
        # gate — it only ever adds score on top of a real asset/route signal,
        # never triggers a match on its own.
        has_livewhale_evidence = bool(livewhale_assets) or route_reference
        if not has_livewhale_evidence:
            return PatternDetectionResult(
                pattern_name=None,
                confidence=0.0,
                evidence=evidence,
                discovered_endpoints=tuple(discovered),
                browser_required=_browser_required_evidence(soup, response),
                warnings=(),
                detector_version=DETECTOR_VERSION,
                needs_review=True,
            )

        confidence = min(0.95, score)
        return PatternDetectionResult(
            pattern_name="livewhale_json",
            confidence=confidence,
            evidence=evidence,
            discovered_endpoints=tuple(discovered),
            browser_required=False,
            warnings=(),
            detector_version=DETECTOR_VERSION,
            needs_review=confidence < MIN_PATTERN_CONFIDENCE,
        )


def _json_events_result(
    pattern_name: str, document: object, *, browser_required: bool = False
) -> PatternDetectionResult:
    """Shared detection result for the JSON-in-script patterns: a match only
    when the parsed document contains an array scoring as an event list."""
    from app.extraction.inference.json_events import find_event_arrays

    candidates = find_event_arrays(document) if document is not None else []
    best = candidates[0] if candidates else None
    if best is None or best.event_like_rate < _JSON_EVENTS_MIN_CONFIDENCE:
        return PatternDetectionResult(
            pattern_name=None,
            confidence=0.0,
            evidence={"event_arrays_found": len(candidates)},
            discovered_endpoints=(),
            browser_required=browser_required,
            warnings=(),
            detector_version=DETECTOR_VERSION,
            needs_review=True,
        )
    confidence = min(0.9, 0.6 + 0.3 * best.event_like_rate)
    return PatternDetectionResult(
        pattern_name=pattern_name,
        confidence=confidence,
        evidence={
            "events_root": best.path,
            "array_size": best.size,
            "event_like_rate": best.event_like_rate,
            "sample_keys": list(best.sample_keys),
        },
        discovered_endpoints=(),
        browser_required=False,
        warnings=(),
        detector_version=DETECTOR_VERSION,
        needs_review=confidence < MIN_PATTERN_CONFIDENCE,
    )


class EmbeddedJsonDetector:
    def detect(self, response: FetchResponse) -> PatternDetectionResult:
        if _access_denied_detected(response):
            return _blocked_result("access denied or challenge page detected")
        import json as _json

        soup = BeautifulSoup(response.text, "html.parser")
        best_doc = None
        best_len = -1
        for script in soup.find_all("script", attrs={"type": "application/json"}):
            text = script.string or script.get_text()
            if not text or not text.strip():
                continue
            try:
                doc = _json.loads(text)
            except (_json.JSONDecodeError, ValueError):
                continue
            # Prefer the largest JSON block, which is most likely to carry the
            # event list; each is still scored on its merits below.
            if len(text) > best_len:
                best_doc, best_len = doc, len(text)
        return _json_events_result("embedded_json", best_doc)


class InlineJsonEventsDetector:
    """A JSON event list embedded as a plain JS variable assignment
    (`window.eventsListing = [...]`) rather than a `<script type=application/json>`
    block. The literal is parsed with json.loads only — never executed. A match
    requires a substantial, event-like array (title + date keys), so an ordinary
    config/data array cannot trigger it. Sits just above generic HTML in the
    reliability order, so any standard structured pattern still wins."""

    def detect(self, response: FetchResponse) -> PatternDetectionResult:
        if _access_denied_detected(response):
            return _blocked_result("access denied or challenge page detected")
        from app.extraction.patterns.inline_json import find_inline_event_variable

        found = find_inline_event_variable(response.text)
        if found is None or found.event_like_rate < _JSON_EVENTS_MIN_CONFIDENCE:
            return PatternDetectionResult(
                pattern_name=None,
                confidence=0.0,
                evidence={"inline_json_event_array": found is not None},
                discovered_endpoints=(),
                browser_required=False,
                warnings=(),
                detector_version=DETECTOR_VERSION,
                needs_review=True,
            )
        confidence = min(0.9, 0.6 + 0.3 * found.event_like_rate)
        return PatternDetectionResult(
            pattern_name="inline_json_events",
            confidence=confidence,
            evidence={
                "events_root": found.name,
                "array_size": found.size,
                "event_like_rate": found.event_like_rate,
            },
            discovered_endpoints=(),
            browser_required=False,
            warnings=(),
            detector_version=DETECTOR_VERSION,
            needs_review=confidence < MIN_PATTERN_CONFIDENCE,
        )


class NextDataDetector:
    def detect(self, response: FetchResponse) -> PatternDetectionResult:
        if _access_denied_detected(response):
            return _blocked_result("access denied or challenge page detected")
        from app.extraction.patterns.next_data import parse_next_data

        return _json_events_result("next_data", parse_next_data(response.text))


class NuxtPayloadDetector:
    def detect(self, response: FetchResponse) -> PatternDetectionResult:
        if _access_denied_detected(response):
            return _blocked_result("access denied or challenge page detected")
        from app.extraction.patterns.nuxt_payload import parse_nuxt_data_script

        # Detection requires a Nuxt-specific signal (the __NUXT_DATA__ script);
        # a bare JSON body has no such marker and must not be claimed here.
        document = parse_nuxt_data_script(response.text)
        # A Nuxt page whose state is only a JS assignment (no parseable
        # payload) is browser_required, deferred to Phase 9 — never eval'd.
        browser_required = document is None and (
            "window.__NUXT__" in response.text or 'id="__nuxt"' in response.text
        )
        return _json_events_result("nuxt_payload", document, browser_required=browser_required)


class IcsCalendarDetector:
    def detect(self, response: FetchResponse) -> PatternDetectionResult:
        if _access_denied_detected(response):
            return _blocked_result("access denied or challenge page detected")
        text = response.text
        content_type = (response.content_type or "").lower()
        looks_ics = "begin:vcalendar" in text[:2000].lower() or "text/calendar" in content_type
        vevents = text.lower().count("begin:vevent")
        if not looks_ics or vevents == 0:
            return PatternDetectionResult(
                pattern_name=None, confidence=0.0, evidence={"vevent_count": vevents},
                discovered_endpoints=(), browser_required=False, warnings=(),
                detector_version=DETECTOR_VERSION, needs_review=True,
            )
        return PatternDetectionResult(
            pattern_name="ics_calendar",
            confidence=0.9,
            evidence={"vevent_count": vevents},
            discovered_endpoints=(),
            browser_required=False,
            warnings=(),
            detector_version=DETECTOR_VERSION,
            needs_review=False,
        )


class RssAtomDetector:
    def detect(self, response: FetchResponse) -> PatternDetectionResult:
        if _access_denied_detected(response):
            return _blocked_result("access denied or challenge page detected")
        head = response.text[:4000].lower()
        is_rss = "<rss" in head or "<rdf:rdf" in head
        is_atom = "<feed" in head and "atom" in head
        if not (is_rss or is_atom):
            return PatternDetectionResult(
                pattern_name=None, confidence=0.0, evidence={},
                discovered_endpoints=(), browser_required=False, warnings=(),
                detector_version=DETECTOR_VERSION, needs_review=True,
            )
        item_count = response.text.lower().count("<item") + response.text.lower().count("<entry")
        if item_count == 0:
            return PatternDetectionResult(
                pattern_name=None, confidence=0.0, evidence={"item_count": 0},
                discovered_endpoints=(), browser_required=False, warnings=(),
                detector_version=DETECTOR_VERSION, needs_review=True,
            )
        # A feed is detectable, but a generic feed may carry no event date;
        # that is resolved (often as needs_review) by the proposer/preview, not
        # asserted here. Confidence is moderate to reflect that.
        return PatternDetectionResult(
            pattern_name="rss_atom_events",
            confidence=0.72,
            evidence={"feed_kind": "atom" if is_atom else "rss", "item_count": item_count},
            discovered_endpoints=(),
            browser_required=False,
            warnings=(),
            detector_version=DETECTOR_VERSION,
            needs_review=False,
        )


class AlgoliaSearchDetector:
    """Matches an Algolia *query response* — the direct case where the
    configured endpoint returns `{"hits": [...], "nbHits": ...}`. A raw HTML
    page merely using Algolia is not actionable without a key, so it is not
    claimed here."""

    def detect(self, response: FetchResponse) -> PatternDetectionResult:
        if _access_denied_detected(response):
            return _blocked_result("access denied or challenge page detected")
        try:
            payload = json.loads(response.text)
        except (json.JSONDecodeError, ValueError):
            payload = None
        if (
            isinstance(payload, dict)
            and isinstance(payload.get("hits"), list)
            and ("nbHits" in payload or "nbPages" in payload)
        ):
            return PatternDetectionResult(
                pattern_name="algolia_search",
                confidence=0.85,
                evidence={"hit_count": len(payload["hits"]), "nb_pages": payload.get("nbPages")},
                discovered_endpoints=(),
                browser_required=False,
                warnings=(),
                detector_version=DETECTOR_VERSION,
                needs_review=False,
            )
        return PatternDetectionResult(
            pattern_name=None, confidence=0.0, evidence={},
            discovered_endpoints=(), browser_required=False, warnings=(),
            detector_version=DETECTOR_VERSION, needs_review=True,
        )


_SIMPLEVIEW_ID_KEYS = ("recid", "_id", "id")
_SIMPLEVIEW_DATE_KEYS = ("startDate", "date", "endDate")


def _simpleview_records(payload: object) -> list[dict] | None:
    """The event-record array of a Simpleview response, at `docs.docs` (an
    outer `docs` object wrapping the inner `docs` list) or a bare `docs` list.
    Returns None when the shape is absent — never guesses from an unrelated
    array."""
    if not isinstance(payload, dict):
        return None
    docs = payload.get("docs")
    if isinstance(docs, dict) and isinstance(docs.get("docs"), list):
        inner = docs["docs"]
    elif isinstance(docs, list):
        inner = docs
    else:
        return None
    return [r for r in inner if isinstance(r, dict)]


def _looks_like_simpleview_event(record: dict) -> bool:
    has_id = any(
        isinstance(record.get(k), (str, int)) and str(record.get(k)).strip()
        for k in _SIMPLEVIEW_ID_KEYS
    )
    has_date = any(record.get(k) for k in _SIMPLEVIEW_DATE_KEYS)
    has_title = bool(record.get("title"))
    return has_id and has_date and has_title


class SimpleviewEventsDetector:
    """Matches a Simpleview event *API response* by its structure — a nested
    `docs.docs` record array whose objects carry a stable id (recid/_id/id), an
    event date (startDate/date/endDate), and a title. Deliberately does NOT
    match on a URL containing `rest_v2`, the word "events", a Simpleview footer,
    or a single generic JSON object; the shape is the whole signal."""

    def detect(self, response: FetchResponse) -> PatternDetectionResult:
        if _access_denied_detected(response):
            return _blocked_result("access denied or challenge page detected")
        try:
            payload = json.loads(response.text)
        except (json.JSONDecodeError, ValueError):
            return PatternDetectionResult(
                pattern_name=None, confidence=0.0, evidence={},
                discovered_endpoints=(), browser_required=False, warnings=(),
                detector_version=DETECTOR_VERSION, needs_review=False,
            )

        records = _simpleview_records(payload)
        if records is None:
            # No docs.docs array at all (e.g. the aggregate/facet endpoint).
            return PatternDetectionResult(
                pattern_name=None, confidence=0.0, evidence={},
                discovered_endpoints=(), browser_required=False, warnings=(),
                detector_version=DETECTOR_VERSION, needs_review=False,
            )
        if not records:
            # The shape is right but there are no records — say so honestly
            # rather than claim a confident match.
            return PatternDetectionResult(
                pattern_name=None, confidence=0.0,
                evidence={"record_path": "docs.docs", "record_count": 0},
                discovered_endpoints=(), browser_required=False,
                warnings=("simpleview_docs_empty",),
                detector_version=DETECTOR_VERSION, needs_review=True,
            )

        sample = records[:5]
        event_like = [r for r in sample if _looks_like_simpleview_event(r)]
        if not event_like or len(event_like) < (len(sample) + 1) // 2:
            # A docs.docs array that isn't event-like is not a Simpleview event
            # source — no false positive on generic nested JSON.
            return PatternDetectionResult(
                pattern_name=None, confidence=0.0,
                evidence={"record_path": "docs.docs", "event_like_sample": len(event_like)},
                discovered_endpoints=(), browser_required=False, warnings=(),
                detector_version=DETECTOR_VERSION, needs_review=False,
            )

        first = event_like[0]
        id_field = next((k for k in _SIMPLEVIEW_ID_KEYS if first.get(k) not in (None, "")), None)
        docs_meta = payload.get("docs") if isinstance(payload.get("docs"), dict) else {}
        total = docs_meta.get("count") if isinstance(docs_meta, dict) else None
        confidence = round(min(0.92, 0.7 + 0.05 * len(event_like)), 4)
        return PatternDetectionResult(
            pattern_name="simpleview_events",
            confidence=confidence,
            evidence={
                "json_shape_match": True,
                "record_path": "docs.docs",
                "record_count": len(records),
                "sample_event_count": len(event_like),
                "id_field": id_field,
                "total_count_metadata": total,
                "request_replay": "single_response_http",
            },
            discovered_endpoints=(response.final_url,),
            browser_required=False,
            warnings=() if total is not None else ("simpleview_pagination_unconfirmed",),
            detector_version=DETECTOR_VERSION,
            needs_review=False,
        )


def run_all_detectors(response: FetchResponse) -> dict[str, PatternDetectionResult]:
    """Run every registered detector over `response` and return each raw result
    keyed by pattern name. `run_detection` selects a single winner from these;
    callers that need to compare specific detectors against each other (e.g.
    the browser path preferring a page-embedded pattern over one that would
    fetch a separate, likely-blocked API) use the raw results directly."""
    detectors: dict[str, PatternDetector] = {
        "the_events_calendar": TheEventsCalendarDetector(),
        "livewhale_json": LiveWhaleDetector(),
        "simpleview_events": SimpleviewEventsDetector(),
        "wordpress_rest": WordPressRestDetector(),
        "json_ld_event": JsonLdDetector(),
        "next_data": NextDataDetector(),
        "nuxt_payload": NuxtPayloadDetector(),
        "embedded_json": EmbeddedJsonDetector(),
        "inline_json_events": InlineJsonEventsDetector(),
        "ics_calendar": IcsCalendarDetector(),
        "rss_atom_events": RssAtomDetector(),
        "algolia_search": AlgoliaSearchDetector(),
        "generic_html_cards": StaticHtmlDetector(),
    }
    return {name: detector.detect(response) for name, detector in detectors.items()}


def run_detection(
    response: FetchResponse, *, min_confidence: float = MIN_PATTERN_CONFIDENCE
) -> PatternDetectionResult:
    results = run_all_detectors(response)
    matched = [
        (name, result) for name, result in results.items() if result.pattern_name is not None
    ]

    def _detector_summary(result: PatternDetectionResult) -> dict:
        # Per-detector confidence/needs_review/browser_required alongside its
        # own evidence — the detection-review screen needs every detector's
        # confidence, not just the eventual winner's.
        return {
            "confidence": result.confidence,
            "needs_review": result.needs_review,
            "browser_required": result.browser_required,
            **result.evidence,
        }

    if not matched:
        all_warnings = tuple(w for r in results.values() for w in r.warnings)
        merged_evidence = {name: _detector_summary(r) for name, r in results.items()}
        browser_required = any(r.browser_required for r in results.values())
        discovered = tuple(e for r in results.values() for e in r.discovered_endpoints)
        return PatternDetectionResult(
            pattern_name=None,
            confidence=0.0,
            evidence=merged_evidence,
            discovered_endpoints=discovered,
            browser_required=browser_required,
            warnings=all_warnings,
            detector_version=DETECTOR_VERSION,
            needs_review=True,
        )

    # Fixed reliability order breaks ties — never a domain conditional.
    matched.sort(key=lambda item: (-item[1].confidence, RELIABILITY_ORDER.index(item[0])))
    winner_name, winner = matched[0]
    needs_review = winner.confidence < min_confidence
    merged_evidence = {
        "winner": winner_name,
        "all_results": {name: _detector_summary(r) for name, r in results.items()},
    }
    return PatternDetectionResult(
        pattern_name=winner.pattern_name if not needs_review else None,
        confidence=winner.confidence,
        evidence=merged_evidence,
        discovered_endpoints=winner.discovered_endpoints,
        browser_required=winner.browser_required,
        warnings=winner.warnings,
        detector_version=DETECTOR_VERSION,
        needs_review=needs_review,
    )
