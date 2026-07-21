"""Pattern detection. Never fetches beyond what's necessary (exactly the one
FetchResponse it's given), never saves events. Each detector inspects the
response and returns a PatternDetectionResult; run_detection() picks a
winner using a fixed reliability order as the only tiebreak — never a
domain-name conditional.
"""

from __future__ import annotations

import json
import re
from typing import Protocol
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from app.extraction.types import FetchResponse, PatternDetectionResult

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
    "wordpress_rest",
    "json_ld_event",
    "generic_html_cards",
)

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
    rf"|\b{_MONTH_NAME}\s*[/|·.-]\s*\d{{1,2}}\b",
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
            for node in data if isinstance(data, list) else [data]:
                nodes_to_check = (
                    node.get("@graph", [])
                    if isinstance(node, dict) and isinstance(node.get("@graph"), list)
                    else ([node] if isinstance(node, dict) else [])
                )
                for candidate_node in nodes_to_check:
                    if not isinstance(candidate_node, dict):
                        continue
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

        generator = soup.find("meta", attrs={"name": "generator"})
        if generator and _WP_GENERATOR_RE.search(str(generator.get("content", ""))):
            evidence["generator_meta"] = generator.get("content")
            score += 0.4

        api_link = soup.find("link", attrs={"rel": "https://api.w.org/"})
        if api_link and api_link.get("href"):
            discovered.append(urljoin(response.final_url, api_link["href"]))
            evidence["wp_json_discovery_link"] = discovered[-1]
            score += 0.4

        link_header = response.headers.get("link", "")
        if "wp-json" in link_header or 'rel="https://api.w.org/"' in link_header:
            score += 0.2
            evidence["link_header_hint"] = True

        script_srcs = [s.get("src", "") for s in soup.find_all("script", src=True)]
        if any("wp-content" in src or "wp-includes" in src for src in script_srcs):
            evidence["wp_script_paths"] = True
            score = min(1.0, score + 0.1)

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


def run_detection(
    response: FetchResponse, *, min_confidence: float = MIN_PATTERN_CONFIDENCE
) -> PatternDetectionResult:
    detectors: dict[str, PatternDetector] = {
        "the_events_calendar": TheEventsCalendarDetector(),
        "livewhale_json": LiveWhaleDetector(),
        "wordpress_rest": WordPressRestDetector(),
        "json_ld_event": JsonLdDetector(),
        "generic_html_cards": StaticHtmlDetector(),
    }
    results = {name: detector.detect(response) for name, detector in detectors.items()}
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
