"""Building the bounded, sanitized evidence sent to a provider.

What leaves the process is deliberately small and structural. The rules are:

* a bounded number of event-card samples, each truncated
* deterministic candidate selectors and detector evidence
* the attempted date formats and the validation failures that made
  deterministic inference give up
* the restricted set of allowed pattern names

What never leaves the process: cookies, Authorization or any request headers,
credentials, audit IP addresses, private notes, the full unbounded document,
personal data beyond what a public event card already shows, and any
local/private-network detail. Sample HTML has `<script>`/`<style>`/`<iframe>`
and every attribute except a short structural allow-list stripped, so a
tracking pixel or inline token cannot ride along.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, Tag

from app.config import get_settings
from app.extraction.inference.html_fields import infer_container, sample_cards
from app.extraction.inference.policy import DEFAULT_POLICY
from app.extraction.inference.selectors import candidate_selectors
from app.extraction.types import FetchResponse, PatternDetectionResult
from app.services.ai.types import AIConfigurationEvidence

# Attributes worth keeping on a sampled element because they are structural
# clues an assistant needs; everything else (style, data-*, on*, src of
# tracking pixels, ...) is dropped.
_KEEP_ATTRS: frozenset[str] = frozenset({"class", "itemprop", "datetime", "rel"})
_STRIP_TAGS: frozenset[str] = frozenset({"script", "style", "iframe", "noscript", "svg", "object"})


def _sanitize(card: Tag, *, max_chars: int) -> str:
    clone = BeautifulSoup(str(card), "html.parser")
    for tag in clone.find_all(_STRIP_TAGS):
        tag.decompose()
    for tag in clone.find_all(True):
        # href is kept but reduced to its path shape, never a full URL with a
        # query string that might carry a token.
        preserved = {}
        if tag.name == "a" and tag.has_attr("href"):
            preserved["href"] = re.sub(r"[?#].*$", "", str(tag["href"]))[:200]
        for attr in list(tag.attrs):
            if attr not in _KEEP_ATTRS:
                del tag[attr]
        tag.attrs.update(preserved)
    text = clone.decode()
    return text[:max_chars]


def build_evidence(
    *,
    response: FetchResponse,
    detection: PatternDetectionResult,
    listing_url: str,
    validation_failures: tuple[str, ...],
    allowed_pattern_names: tuple[str, ...],
) -> AIConfigurationEvidence:
    settings = get_settings()
    soup = BeautifulSoup(response.text, "html.parser")

    sample_html: list[str] = []
    candidate_map: dict[str, list[str]] = {}
    container = infer_container(soup, DEFAULT_POLICY)
    if container is not None:
        all_cards = sample_cards(soup, container.selector, DEFAULT_POLICY)
        cards = all_cards[: settings.ai_max_sample_cards]
        remaining = settings.ai_max_evidence_chars
        for card in cards:
            html = _sanitize(card, max_chars=min(remaining, 4000))
            if not html:
                continue
            sample_html.append(html)
            remaining -= len(html)
            if remaining <= 0:
                break
        # A compact catalogue of stable selectors seen in the first card, so
        # the assistant proposes from real options rather than inventing them.
        if cards:
            seen: dict[str, list[str]] = {}
            for element in cards[0].find_all(True):
                for candidate in candidate_selectors(cards[0], element, DEFAULT_POLICY):
                    seen.setdefault(candidate.tag_name, [])
                    if candidate.selector not in seen[candidate.tag_name]:
                        seen[candidate.tag_name].append(candidate.selector)
            candidate_map = {tag: sels[:8] for tag, sels in list(seen.items())[:12]}

    evidence = detection.evidence if isinstance(detection.evidence, dict) else {}
    detector_evidence = {
        k: v for k, v in evidence.items() if k in ("winner", "all_results")
    } or {"summary": str(evidence)[:1000]}

    pagination = {
        "has_rel_next": bool(soup.find("a", attrs={"rel": "next"})),
        "has_pagination_class": bool(soup.select_one("[class*=pagination], [class*=pager]")),
    }

    return AIConfigurationEvidence(
        listing_url=listing_url,
        detected_pattern=detection.pattern_name,
        detector_evidence=detector_evidence,
        sample_cards_html=tuple(sample_html),
        candidate_selectors=candidate_map,
        attempted_date_formats=tuple(_ATTEMPTED_DATE_FORMATS),
        validation_failures=validation_failures,
        pagination_indicators=pagination,
        allowed_pattern_names=allowed_pattern_names,
    )


# The date formats deterministic inference already tried, so the assistant
# knows what did not work rather than re-proposing it.
from app.extraction.inference.dates import DATE_FORMAT_TABLE as _DATE_FORMAT_TABLE  # noqa: E402

_ATTEMPTED_DATE_FORMATS = _DATE_FORMAT_TABLE[:12]
