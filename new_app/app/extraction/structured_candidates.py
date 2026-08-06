"""Deterministic scoring of browser-observed responses as structured event
sources.

Browser recovery sees every response a page fetched: the site's own event API,
unrelated first-party JSON, and a lot of third-party telemetry (analytics, ad
pixels, social beacons, map tiles). The old logic only preferred an observed
JSON response when an *existing* detector recognised it — so a first-party event
API in an unknown format lost to a zero-result rendered-HTML proposal.

This module ranks candidates by *evidence*, not by whether a detector already
knows the shape:

* ownership — same registrable domain as the listing page (validated, not a
  string-prefix guess),
* an event-record array with event-like fields (title/date/url/id/venue/…),
* structural signals (record count, pagination/total metadata),
  minus penalties for telemetry shapes, config/aggregate-only objects, and
  empty responses.

Everything here is pure, deterministic, and site-agnostic — no hostname,
domain, or institution literal appears anywhere; the target site is only ever
compared against *its own* listing origin.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

# --- classifications ---------------------------------------------------------

FIRST_PARTY_EVENT_CANDIDATE = "first_party_event_candidate"
FIRST_PARTY_OTHER = "first_party_other"
THIRD_PARTY_FUNCTIONAL = "third_party_functional"
THIRD_PARTY_TELEMETRY = "third_party_telemetry"

# A candidate must clear this event-likeness score to be treated as a viable
# structured extraction source (as opposed to unrelated first-party JSON).
EVENT_CANDIDATE_THRESHOLD = 0.45

# Bounds for the safe, persisted inspection metadata.
_MAX_SAMPLE_FIELDS = 30
_MAX_TOP_KEYS = 30
_MAX_INSPECT_BYTES = 2_000_000

# --- registrable-domain (first-party) ---------------------------------------

# Common multi-label public suffixes, so "bbc.co.uk" resolves to "bbc.co.uk"
# rather than "co.uk". Not the full Public Suffix List — a pragmatic subset
# covering the suffixes most likely to appear; the plain last-two-labels rule
# handles everything else (including all gTLDs like .com/.org/.travel).
_MULTI_LABEL_SUFFIXES = frozenset(
    {
        "co.uk", "org.uk", "gov.uk", "ac.uk", "me.uk", "ltd.uk", "plc.uk",
        "com.au", "net.au", "org.au", "gov.au", "edu.au", "co.nz", "org.nz",
        "co.za", "co.jp", "or.jp", "ne.jp", "com.br", "com.mx", "com.ar",
        "co.in", "net.in", "org.in", "co.kr", "com.cn", "com.sg", "com.hk",
        "co.il", "com.tr", "com.tw", "com.ua", "co.id", "com.ph", "com.my",
    }
)


def _host_of(url_or_host: str) -> str:
    value = (url_or_host or "").strip()
    value = urlsplit(value).hostname or "" if "://" in value else value.split("/")[0]
    return value.lower().rstrip(".")


def registrable_domain(url_or_host: str) -> str | None:
    """The registrable domain (eTLD+1) of a URL or hostname, or None. IP
    literals are returned unchanged so two requests to the same IP compare
    equal."""
    host = _host_of(url_or_host)
    if not host:
        return None
    # Bare IPv4/IPv6 — treat the literal as its own domain.
    if host.replace(".", "").isdigit() or ":" in host:
        return host
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    last_two = ".".join(labels[-2:])
    if last_two in _MULTI_LABEL_SUFFIXES:
        return ".".join(labels[-3:])
    return last_two


def same_registrable_domain(url_a: str, url_b: str) -> bool:
    domain_a = registrable_domain(url_a)
    domain_b = registrable_domain(url_b)
    return domain_a is not None and domain_a == domain_b


# --- third-party / telemetry detection --------------------------------------

# Registrable domains whose traffic is analytics/advertising/social/consent/
# map/error telemetry — never an event source. Matched on the registrable
# domain so subdomains (e.g. www.google-analytics.com) are covered.
_TELEMETRY_DOMAINS = frozenset(
    {
        "google-analytics.com", "googletagmanager.com", "googlesyndication.com",
        "googleadservices.com", "doubleclick.net", "google.com", "gstatic.com",
        "googleapis.com", " google.co", "g.doubleclick.net", "adservice.google.com",
        "spotify.com", "scdn.co", "spotifycdn.com",
        "pinterest.com", "pinimg.com", "ct.pinterest.com",
        "facebook.com", "facebook.net", "fbcdn.net", "instagram.com",
        "twitter.com", "x.com", "t.co", "ads-twitter.com",
        "tiktok.com", "linkedin.com", "licdn.com", "snapchat.com", "bing.com",
        "segment.com", "segment.io", "mixpanel.com", "amplitude.com",
        "hotjar.com", "clarity.ms", "quantserve.com", "scorecardresearch.com",
        "newrelic.com", "nr-data.net", "bugsnag.com", "sentry.io", "sentry-cdn.com",
        "cloudflareinsights.com", "cookielaw.org", "onetrust.com", "cookiebot.com",
        "criteo.com", "taboola.com", "outbrain.com", "branch.io", "optimizely.com",
        "cloudfront.net", "cdn.jsdelivr.net", "cdnjs.cloudflare.com", "unpkg.com",
    }
)

# Host-fragment and path signals of telemetry even on an otherwise-unknown host.
_TELEMETRY_HOST_MARKERS = (
    "analytics", "telemetry", "tracking", "tracker", "pixel", "beacon",
    "metrics", "adservice", "doubleclick", "pagead", "adserver", "stats.",
)
_TELEMETRY_PATH_MARKERS = (
    "/collect", "/g/collect", "/pixel", "/beacon", "/track", "/telemetry",
    "/analytics", "/pagead", "/metrics", "/gtm", "/gtag", "/ingest", "/b/ss",
    "/i/adsct", "/tr", "/log", "/event/track", "/rum",
)


def is_telemetry(url: str) -> bool:
    host = _host_of(url)
    domain = registrable_domain(url) or ""
    path = (urlsplit(url).path or "").lower()
    if domain in _TELEMETRY_DOMAINS:
        return True
    if any(marker in host for marker in _TELEMETRY_HOST_MARKERS):
        return True
    return any(path.startswith(marker) or path == marker for marker in _TELEMETRY_PATH_MARKERS)


# --- event-likeness of a JSON payload ---------------------------------------

_EVENT_PATH_TERMS = ("event", "calendar", "listing", "occurrence", "happening", "whatson")
_AGGREGATE_PATH_TERMS = ("aggregate", "facet", "count", "summary", "filter", "meta")

_TITLE_FIELDS = frozenset({"title", "name", "eventname", "headline", "label", "summary"})
_DATE_FIELDS = frozenset(
    {
        "startdate", "start", "startdatetime", "begindate", "date", "dates",
        "eventdate", "datetime", "starttime", "start_date", "startdateutc",
    }
)
_ENDDATE_FIELDS = frozenset({"enddate", "end", "enddatetime", "finishdate", "end_date"})
_URL_FIELDS = frozenset(
    {"url", "link", "detailurl", "permalink", "canonicalurl", "href", "eventurl", "slug"}
)
_ID_FIELDS = frozenset({"id", "eventid", "uid", "objectid", "guid", "recordid"})
_VENUE_FIELDS = frozenset(
    {"venue", "location", "place", "locationname", "venuename", "address", "venueid"}
)
_CATEGORY_FIELDS = frozenset({"category", "categories", "type", "eventtype", "tags", "genre"})
_IMAGE_FIELDS = frozenset({"image", "imageurl", "photo", "thumbnail", "media", "img"})
_COUNT_KEYS = frozenset(
    {"total", "totalcount", "count", "numfound", "nbhits", "totalresults", "pagecount", "pages"}
)


def _norm(key: str) -> str:
    return "".join(ch for ch in key.lower() if ch.isalnum())


def _record_arrays(obj, path: str = "", depth: int = 0):
    """Yield (dot_path, list-of-dicts) for arrays of objects at or below `obj`,
    searching a bounded number of levels so a deeply-nested payload can't cause
    unbounded work."""
    if depth > 4:
        return
    if isinstance(obj, list):
        if obj and sum(isinstance(item, dict) for item in obj) >= max(1, len(obj) // 2):
            yield path, [item for item in obj if isinstance(item, dict)]
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            child = f"{path}.{key}" if path else str(key)
            yield from _record_arrays(value, child, depth + 1)


def _field_groups_present(records: list[dict]) -> dict[str, bool]:
    keys: set[str] = set()
    for record in records[:25]:
        keys.update(_norm(k) for k in record)
        # one level into nested dicts, so venue.name / location.address count
        for value in record.values():
            if isinstance(value, dict):
                keys.update(_norm(k) for k in value)
    return {
        "title": bool(keys & _TITLE_FIELDS),
        "date": bool(keys & _DATE_FIELDS),
        "enddate": bool(keys & _ENDDATE_FIELDS),
        "url": bool(keys & _URL_FIELDS),
        "id": bool(keys & _ID_FIELDS),
        "venue": bool(keys & _VENUE_FIELDS),
        "category": bool(keys & _CATEGORY_FIELDS),
        "image": bool(keys & _IMAGE_FIELDS),
    }


def _has_count_metadata(payload) -> bool:
    if not isinstance(payload, dict):
        return False
    return any(_norm(k) in _COUNT_KEYS for k in payload)


@dataclass
class CandidateAnalysis:
    """Bounded, redacted description of one observed response. Everything here
    is safe to persist — no full body, cookies, headers, tokens, or raw query
    values."""

    sanitized_url: str
    origin: str | None
    classification: str
    first_party: bool
    content_type: str
    byte_size: int
    top_level_type: str
    top_level_keys: list[str] = field(default_factory=list)
    record_array_path: str | None = None
    sample_field_names: list[str] = field(default_factory=list)
    sample_record_count: int = 0
    event_likeness_score: float = 0.0
    score_reasons: list[str] = field(default_factory=list)
    reject_reason: str | None = None
    # Bounded, redacted request/response metadata for recurring HTTP replay.
    request_method: str | None = None
    request_content_type: str | None = None
    request_body: str | None = None
    query_param_names: list[str] = field(default_factory=list)
    response_status: int | None = None
    # Full request URL (query included) and safe headers — needed to build a
    # request recipe that preserves the nested json/token params and Referer.
    # These may carry a *public* token, so they live only in request_metadata
    # (which flows into the stored, redacted recipe) — never in to_evidence().
    request_url: str | None = None
    request_headers: dict[str, str] = field(default_factory=dict)

    @property
    def request_metadata(self) -> dict:
        return {
            "method": self.request_method,
            "request_content_type": self.request_content_type,
            "request_body": self.request_body,
            "query_param_names": list(self.query_param_names),
            "request_url": self.request_url,
            "request_headers": dict(self.request_headers),
        }

    @property
    def is_event_candidate(self) -> bool:
        return (
            self.classification == FIRST_PARTY_EVENT_CANDIDATE
            and self.event_likeness_score >= EVENT_CANDIDATE_THRESHOLD
        )

    def to_evidence(self) -> dict:
        return {
            "url": self.sanitized_url,
            "origin": self.origin,
            "classification": self.classification,
            "first_party": self.first_party,
            "content_type": self.content_type,
            "byte_size": self.byte_size,
            "top_level_type": self.top_level_type,
            "top_level_keys": self.top_level_keys,
            "record_array_path": self.record_array_path,
            "sample_field_names": self.sample_field_names,
            "sample_record_count": self.sample_record_count,
            "event_likeness_score": round(self.event_likeness_score, 4),
            "score_reasons": self.score_reasons,
            "reject_reason": self.reject_reason,
            "request_method": self.request_method,
            "request_content_type": self.request_content_type,
            "query_param_names": list(self.query_param_names),
            "response_status": self.response_status,
        }


def sanitize_url(url: str) -> str:
    """URL with the query and fragment stripped — a discovered endpoint is
    worth recording, but its query may carry keys/tokens."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _score(payload, url: str, listing_url: str, first_party: bool):
    """Deterministic event-likeness in [0, 1] with human-readable reasons."""
    reasons: list[str] = []
    score = 0.0
    path = (urlsplit(url).path or "").lower()

    if first_party:
        score += 0.25
        reasons.append("same registrable domain as listing (first-party)")
    else:
        reasons.append("cross-origin (not first-party)")

    if any(term in path for term in _EVENT_PATH_TERMS):
        score += 0.1
        reasons.append("endpoint path contains an event term")

    arrays = list(_record_arrays(payload))
    best_path, best_records, best_groups, best_hits = None, [], {}, -1
    for arr_path, records in arrays:
        groups = _field_groups_present(records)
        hits = sum(groups.values())
        if hits > best_hits or (hits == best_hits and len(records) > len(best_records)):
            best_path, best_records, best_groups, best_hits = arr_path, records, groups, hits

    if best_records:
        if len(best_records) >= 2:
            score += 0.15
            reasons.append(f"{len(best_records)} record-like objects")
        else:
            score += 0.05
            reasons.append("single record-like object")
        weights = {
            "title": 0.12, "date": 0.12, "url": 0.06, "id": 0.06,
            "venue": 0.05, "category": 0.03, "image": 0.03, "enddate": 0.02,
        }
        for group, present in best_groups.items():
            if present:
                score += weights.get(group, 0.0)
                reasons.append(f"records carry {group}")
    else:
        reasons.append("no record-like array of objects")

    if _has_count_metadata(payload):
        score += 0.05
        reasons.append("total/count pagination metadata present")

    # Penalties: aggregate/facet-only shapes and empty payloads.
    if any(term in path for term in _AGGREGATE_PATH_TERMS) and best_hits < 3:
        score -= 0.15
        reasons.append("aggregate/metadata endpoint without event records (penalised)")
    if payload in (None, {}, []):
        score -= 0.3
        reasons.append("empty response (penalised)")

    return max(0.0, min(1.0, score)), best_path, best_records, best_groups, reasons


def analyze_response(
    *,
    url: str,
    payload,
    listing_url: str,
    content_type: str = "application/json",
    raw_text: str = "",
    request_meta: dict | None = None,
) -> CandidateAnalysis:
    """Classify and score a single observed JSON response into a bounded,
    persistable analysis. Never stores the response body. `request_meta` is the
    browser's bounded request metadata for this URL, if any."""
    origin = registrable_domain(url)
    first_party = same_registrable_domain(url, listing_url)
    byte_size = len(raw_text.encode("utf-8")) if raw_text else len(json.dumps(payload, default=str))

    if isinstance(payload, list):
        top_level_type = "list"
        top_level_keys: list[str] = []
    elif isinstance(payload, dict):
        top_level_type = "object"
        top_level_keys = sorted(str(k) for k in payload)[:_MAX_TOP_KEYS]
    else:
        top_level_type = "scalar"
        top_level_keys = []

    analysis = CandidateAnalysis(
        sanitized_url=sanitize_url(url),
        origin=origin,
        classification=FIRST_PARTY_OTHER,
        first_party=first_party,
        content_type=content_type,
        byte_size=byte_size,
        top_level_type=top_level_type,
        top_level_keys=top_level_keys,
    )

    if request_meta:
        analysis.request_method = request_meta.get("method")
        analysis.request_content_type = request_meta.get("request_content_type")
        body = request_meta.get("request_body")
        analysis.request_body = body if isinstance(body, str) else None
        names = request_meta.get("query_param_names")
        analysis.query_param_names = list(names) if isinstance(names, list) else []
        analysis.response_status = request_meta.get("response_status")
        req_url = request_meta.get("request_url")
        analysis.request_url = req_url if isinstance(req_url, str) else None
        headers = request_meta.get("request_headers")
        analysis.request_headers = dict(headers) if isinstance(headers, dict) else {}

    if byte_size > _MAX_INSPECT_BYTES:
        analysis.classification = THIRD_PARTY_FUNCTIONAL if not first_party else FIRST_PARTY_OTHER
        analysis.reject_reason = "response exceeds the inspection size bound"
        return analysis

    if is_telemetry(url):
        analysis.classification = THIRD_PARTY_TELEMETRY
        analysis.reject_reason = "telemetry/advertising/tracking endpoint"
        return analysis

    if not first_party:
        analysis.classification = THIRD_PARTY_FUNCTIONAL
        analysis.reject_reason = "cross-origin third-party endpoint"
        return analysis

    score, record_path, records, _groups, reasons = _score(payload, url, listing_url, first_party)
    analysis.event_likeness_score = score
    analysis.score_reasons = reasons
    analysis.record_array_path = record_path
    analysis.sample_record_count = len(records)
    if records:
        analysis.sample_field_names = sorted(str(k) for k in records[0])[:_MAX_SAMPLE_FIELDS]

    if score >= EVENT_CANDIDATE_THRESHOLD:
        analysis.classification = FIRST_PARTY_EVENT_CANDIDATE
    else:
        analysis.classification = FIRST_PARTY_OTHER
        analysis.reject_reason = "first-party JSON but below the event-likeness threshold"
    return analysis
