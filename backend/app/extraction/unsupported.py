"""UnsupportedSiteReporter: assembles the diagnostic record for a website
that no detector could confidently match, or whose fetch was blocked.

Deduplication of unchanged reports (see app.repositories.unsupported_site_report
.should_create_new_report) is driven by `report_fingerprint`, a deterministic
hash of the report's own signature — never a timestamp or run ID, so the
same underlying failure never produces a fresh report every single run.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from bs4 import BeautifulSoup

from app.extraction.types import FetchResponse, PatternDetectionResult


def report_fingerprint(
    website_id: int,
    detection: PatternDetectionResult,
    http_status: int | None,
    failure_reason: str | None,
) -> str:
    signature = {
        "website_id": website_id,
        "evidence": detection.evidence,
        "http_status": http_status,
        "failure_reason": failure_reason,
        "browser_required": detection.browser_required,
    }
    encoded = json.dumps(signature, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class UnsupportedReportData:
    website_id: int
    submitted_url: str
    final_url: str | None
    http_status: int | None
    page_title: str | None
    detected_platform_evidence: dict[str, Any]
    available_detector_results: dict[str, Any]
    discovered_endpoints: list[str]
    browser_required: bool
    json_ld_presence: bool
    pagination_indicators: dict[str, Any]
    access_denied_or_challenge_detected: bool
    failure_reason: str | None
    fingerprint: str


def build_report(
    *,
    website_id: int,
    submitted_url: str,
    response: FetchResponse | None,
    detection: PatternDetectionResult,
    failure_reason: str | None,
) -> UnsupportedReportData:
    page_title: str | None = None
    json_ld_presence = False
    pagination_indicators: dict[str, Any] = {}
    access_denied = True

    if response is not None:
        access_denied = response.blocked_reason is not None
        if not access_denied:
            soup = BeautifulSoup(response.text, "html.parser")
            title_tag = soup.find("title")
            page_title = title_tag.get_text(strip=True) if title_tag else None
            json_ld_presence = bool(soup.find_all("script", attrs={"type": "application/ld+json"}))
            pagination_indicators = {
                "has_next_link": bool(soup.find("a", attrs={"rel": "next"})),
                "has_pagination_class": bool(
                    soup.select_one("[class*=pagination], [class*=pager]")
                ),
            }

    evidence = detection.evidence if isinstance(detection.evidence, dict) else {}
    all_results = evidence.get("all_results", evidence)
    all_results = all_results if isinstance(all_results, dict) else {}

    return UnsupportedReportData(
        website_id=website_id,
        submitted_url=submitted_url,
        final_url=response.final_url if response else None,
        http_status=response.status_code if response else None,
        page_title=page_title,
        detected_platform_evidence=dict(all_results),
        available_detector_results=dict(all_results),
        discovered_endpoints=list(detection.discovered_endpoints),
        browser_required=detection.browser_required,
        json_ld_presence=json_ld_presence,
        pagination_indicators=pagination_indicators,
        access_denied_or_challenge_detected=access_denied,
        failure_reason=failure_reason,
        fingerprint=report_fingerprint(
            website_id, detection, response.status_code if response else None, failure_reason
        ),
    )
