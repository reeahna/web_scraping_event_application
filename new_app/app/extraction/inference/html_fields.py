"""Generic HTML card inference: find the repeated event card, then infer a
selector for each event field from deterministic evidence.

Everything a candidate is scored on is an observation about the *page*, never
about which site it is:

* semantic evidence — class/id/itemprop/aria-label/data-attribute word tokens
  (`selectors.name_tokens`), plus the element's own tag
* schema.org agreement — a matching `itemprop`
* behaviour across the sampled cards — coverage, exactly-one-match-per-card,
  and variation between cards
* parse success — does the value actually work as a date, time, URL, image?
* selector stability — depth and shape (`selectors.selector_stability`)

Candidate values are read through `app.extraction.selectors.resolve_css`, the
same function the extraction pattern uses at run time. A proposal is
therefore scored on exactly the values extraction will later produce — there
is no separate "inference-time" text extraction that could disagree with it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from app.core.url_safety import UnsafeURLError, validate_public_url
from app.extraction.detection import DATE_LIKE_RE
from app.extraction.inference.dates import (
    infer_date_formats,
    infer_time_formats,
    is_all_day,
    normalize_whitespace,
)
from app.extraction.inference.policy import AutoOnboardingPolicy
from app.extraction.inference.selectors import (
    SelectorCandidate,
    candidate_selectors,
    is_stable_class_token,
    selector_stability,
    stable_classes,
)
from app.extraction.inference.types import FieldSelectorCandidate
from app.extraction.selectors import resolve_css

# Field names exactly as patterns/static_html.py resolves them.
ROLES: tuple[str, ...] = (
    "title",
    "canonical_url",
    "start_datetime",
    "start_time",
    "end_datetime",
    "end_time",
    "venue",
    "address",
    "description",
    "image",
    "source_category",
    "external_source_id",
)

# Roles whose value must differ from card to card — a selector returning the
# same title or the same URL for every card is measuring page chrome.
IDENTITY_ROLES: frozenset[str] = frozenset({"title", "canonical_url"})

ROLE_VOCAB: dict[str, frozenset[str]] = {
    "title": frozenset({"title", "name", "headline", "heading", "subject"}),
    "canonical_url": frozenset(
        {"title", "name", "link", "permalink", "url", "headline", "details", "info"}
    ),
    "start_datetime": frozenset({"date", "datetime", "when", "start", "day", "dates"}),
    "start_time": frozenset({"time", "hour", "showtime", "start", "doors", "clock"}),
    "end_datetime": frozenset({"end", "until", "through", "finish"}),
    "end_time": frozenset({"end", "until", "finish"}),
    "venue": frozenset(
        {"venue", "location", "place", "where", "hall", "theater", "theatre", "room", "building"}
    ),
    "address": frozenset({"address", "street", "addr", "postal", "zip"}),
    "description": frozenset(
        {"description", "summary", "excerpt", "teaser", "tagline", "blurb", "subtitle", "caption"}
    ),
    "image": frozenset(
        {"image", "img", "photo", "thumb", "thumbnail", "poster", "picture", "media"}
    ),
    "source_category": frozenset(
        {"category", "categories", "genre", "type", "tag", "tags", "kind", "series", "topic"}
    ),
    "external_source_id": frozenset({"id", "uid", "identifier", "guid"}),
}

ROLE_ANTI_VOCAB: dict[str, frozenset[str]] = {
    "title": frozenset(
        {"date", "time", "venue", "address", "image", "photo", "thumb", "price", "cost", "share"}
    ),
    "canonical_url": frozenset(
        {"nav", "menu", "pagination", "filter", "search", "social", "share", "ical", "google"}
    ),
    "start_datetime": frozenset({"end", "updated", "published", "posted", "expire"}),
    "start_time": frozenset({"end"}),
    "description": frozenset({"date", "time", "price", "cost", "venue", "location", "address"}),
    "image": frozenset({"logo", "icon", "avatar", "sprite"}),
}

ROLE_TAG_PRIOR: dict[str, dict[str, float]] = {
    "title": {"h1": 0.85, "h2": 0.85, "h3": 0.85, "h4": 0.7, "h5": 0.6, "h6": 0.6, "a": 0.45},
    "canonical_url": {"a": 0.8},
    "start_datetime": {"time": 0.9},
    "end_datetime": {"time": 0.6},
    "start_time": {"time": 0.8},
    "end_time": {"time": 0.6},
    "image": {"img": 0.9, "source": 0.7, "picture": 0.6},
    "address": {"address": 0.9},
    "description": {"p": 0.6, "div": 0.35},
}
_DEFAULT_TAG_PRIOR = 0.3

ITEMPROP_ROLES: dict[str, str] = {
    "name": "title",
    "headline": "title",
    "url": "canonical_url",
    "startdate": "start_datetime",
    "enddate": "end_datetime",
    "location": "venue",
    "address": "address",
    "description": "description",
    "image": "image",
    "identifier": "external_source_id",
}

_URL_ATTRIBUTES: frozenset[str] = frozenset({"href"})
_IMAGE_ATTRIBUTES: frozenset[str] = frozenset(
    {"src", "data-src", "data-lazy-src", "data-original", "srcset", "data-srcset", "style"}
)
_DATE_ATTRIBUTES: frozenset[str | None] = frozenset({None, "datetime", "content"})

_IMAGE_EXTENSION_RE = re.compile(r"\.(?:jpe?g|png|webp|gif|avif|svg)(?:[?#]|$)", re.IGNORECASE)
_CSS_URL_RE = re.compile(r"url\((['\"]?)([^'\")]+)\1\)")

# Weights sum to 1.0; selector instability is subtracted afterwards.
_W_SEMANTIC = 0.26
_W_TAG = 0.13
_W_SCHEMA = 0.10
_W_COVERAGE = 0.18
_W_UNIQUE = 0.09
_W_PARSE = 0.16
_W_VARIATION = 0.08
_W_STABILITY_PENALTY = 0.10
_BARE_TAG_PENALTY = 0.04
_BARE_TAG_RE = re.compile(r"[a-z][a-z0-9]*")


# --- Container inference ----------------------------------------------------


@dataclass(frozen=True)
class ContainerCandidate:
    selector: str
    count: int
    confidence: float
    depth: int
    evidence: tuple[str, ...]


def _depth(element: Tag) -> int:
    return sum(1 for _ in element.parents)


def _group_key(tag: Tag) -> tuple[str, tuple[str, ...]] | None:
    classes = stable_classes(tag)
    if not classes:
        return None
    return tag.name, tuple(classes)


def infer_container(soup: BeautifulSoup, policy: AutoOnboardingPolicy) -> ContainerCandidate | None:
    """Picks the repeated element that *is* one event card.

    Where several nested groups repeat (a card and the detail block inside
    it), the outermost of the near-tied group wins — the outer element is the
    one that owns every field, so scoping field inference to it is what makes
    a per-card selector resolvable at all.
    """
    groups: dict[tuple[str, tuple[str, ...]], list[Tag]] = {}
    for tag in soup.find_all(True):
        key = _group_key(tag)
        if key is not None:
            groups.setdefault(key, []).append(tag)

    candidates: list[ContainerCandidate] = []
    for (tag_name, classes), elements in groups.items():
        if len(elements) < policy.min_cards_for_inference:
            continue
        selector = tag_name + "".join(f".{c}" for c in classes)
        matched = soup.select(selector)
        # An overmatching selector would silently pull in unrelated elements.
        if len(matched) != len(elements):
            continue
        # Nested self-matches (a card inside a card) can't be a card list.
        if any(other is not el and other in el.parents for el in elements for other in elements):
            continue

        count = len(elements)
        with_link = sum(1 for el in elements if el.find("a", href=True)) / count
        with_date = sum(
            1 for el in elements if el.find("time") or DATE_LIKE_RE.search(el.get_text(" "))
        ) / count
        with_heading = sum(1 for el in elements if el.find(re.compile(r"^h[1-6]$"))) / count
        with_text = sum(1 for el in elements if len(el.get_text(" ", strip=True)) >= 20) / count
        texts = [el.get_text(" ", strip=True) for el in elements]
        variation = len({t for t in texts if t}) / count

        if with_link < 0.5 or variation < 0.5:
            continue

        confidence = min(
            0.95,
            0.30 * with_link
            + 0.25 * with_date
            + 0.15 * with_heading
            + 0.15 * with_text
            + 0.15 * variation,
        )
        candidates.append(
            ContainerCandidate(
                selector=selector,
                count=count,
                confidence=confidence,
                depth=_depth(elements[0]),
                evidence=(
                    f"{count} repeated elements",
                    f"link coverage {with_link:.2f}",
                    f"date-like coverage {with_date:.2f}",
                    f"heading coverage {with_heading:.2f}",
                    f"content variation {variation:.2f}",
                ),
            )
        )

    if not candidates:
        return None

    best = max(c.confidence for c in candidates)
    near_tied = [c for c in candidates if c.confidence >= best - 0.15]
    near_tied.sort(key=lambda c: (c.depth, -c.confidence, -c.count, len(c.selector)))
    return near_tied[0]


# --- Field inference --------------------------------------------------------


@dataclass(frozen=True)
class _Observation:
    selector: str
    attribute: str | None
    tags: frozenset[str]
    hints: frozenset[str]
    itemprops: frozenset[str]
    first_anchor: bool
    values: tuple[str | None, ...]
    match_counts: tuple[int, ...]

    @property
    def nonempty(self) -> list[str]:
        return [v for v in self.values if v]

    @property
    def coverage(self) -> float:
        return len(self.nonempty) / len(self.values) if self.values else 0.0

    @property
    def unique_match_rate(self) -> float:
        """Measured over the cards the selector matches at all — "absent from
        this card" is a coverage fact, not an ambiguity one, and conflating
        the two would reject a perfectly precise selector just because some
        cards omit the field."""
        matching = [c for c in self.match_counts if c >= 1]
        if not matching:
            return 0.0
        return sum(1 for c in matching if c == 1) / len(matching)

    @property
    def variation(self) -> float:
        values = self.nonempty
        return len(set(values)) / len(values) if values else 0.0


def _as_text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        value = " ".join(str(v) for v in value)
    text = str(value).strip()
    return text or None


def _collect_candidates(
    cards: list[Tag], policy: AutoOnboardingPolicy
) -> dict[tuple[str, str | None], SelectorCandidate]:
    collected: dict[tuple[str, str | None], SelectorCandidate] = {}
    for card in cards:
        per_card = 0
        for element in card.find_all(True):
            if per_card >= policy.max_candidate_selectors_per_card:
                break
            for candidate in candidate_selectors(card, element, policy):
                key = (candidate.selector, candidate.attribute)
                existing = collected.get(key)
                if existing is None:
                    collected[key] = candidate
                else:
                    collected[key] = SelectorCandidate(
                        selector=candidate.selector,
                        attribute=candidate.attribute,
                        tag_name=existing.tag_name,
                        hints=existing.hints | candidate.hints,
                        itemprop=existing.itemprop or candidate.itemprop,
                        is_first_anchor=existing.is_first_anchor or candidate.is_first_anchor,
                    )
                per_card += 1
    return collected


def _observe(
    cards: list[Tag], collected: dict[tuple[str, str | None], SelectorCandidate]
) -> list[_Observation]:
    observations: list[_Observation] = []
    for (selector, attribute), candidate in collected.items():
        values: list[str | None] = []
        counts: list[int] = []
        tags: set[str] = {candidate.tag_name}
        itemprops: set[str] = {candidate.itemprop} if candidate.itemprop else set()
        for card in cards:
            matches = card.select(selector)
            counts.append(len(matches))
            if not matches:
                values.append(None)
                continue
            tags.add(matches[0].name)
            prop = matches[0].get("itemprop")
            if isinstance(prop, str) and prop:
                itemprops.add(prop)
            values.append(_as_text(resolve_css(card, selector, attribute).value))
        observations.append(
            _Observation(
                selector=selector,
                attribute=attribute,
                tags=frozenset(tags),
                hints=candidate.hints,
                itemprops=frozenset(itemprops),
                first_anchor=candidate.is_first_anchor,
                values=tuple(values),
                match_counts=tuple(counts),
            )
        )
    observations.sort(key=lambda o: (o.selector, o.attribute or ""))
    return observations


# --- Per-role value validators ---------------------------------------------


def _rate(values: list[str], predicate) -> float:
    return sum(1 for v in values if predicate(v)) / len(values) if values else 0.0


def _url_is_usable(value: str, base_url: str) -> bool:
    """Same resolution the pattern performs (`urljoin` against the page URL),
    then the same SSRF-safety check EventValidator will apply. A bare
    fragment or empty href is not a usable event URL."""
    text = value.strip()
    if not text or text.startswith("#") or text.lower().startswith("javascript:"):
        return False
    try:
        validate_public_url(urljoin(base_url, text))
    except UnsafeURLError:
        return False
    return True


def _image_is_usable(value: str) -> bool:
    if "url(" in value:
        match = _CSS_URL_RE.search(value)
        return bool(match and match.group(2))
    first = value.split()[0] if value.split() else ""
    return bool(_IMAGE_EXTENSION_RE.search(first) or "/" in first)


def _title_is_usable(value: str) -> bool:
    text = normalize_whitespace(value)
    if not (2 <= len(text) <= 300):
        return False
    # A "title" that is really the card's date is a mis-assignment.
    return not DATE_LIKE_RE.fullmatch(text)


def _parse_success(role: str, values: list[str], base_url: str) -> tuple[float, list[str]]:
    """(rate, evidence). Roles with no parseable form score 1.0 on any
    non-empty value — they are then carried entirely by naming/coverage
    evidence rather than by a parser."""
    if not values:
        return 0.0, []
    if role == "title":
        return _rate(values, _title_is_usable), ["title text within a plausible length"]
    if role in ("canonical_url", "detail_link"):
        return (
            _rate(values, lambda v: _url_is_usable(v, base_url)),
            ["values resolve to a valid public URL"],
        )
    if role in ("start_datetime", "end_datetime"):
        _, rate = infer_date_formats(list(values))
        return rate, ["values parse under an inferred date format"]
    if role in ("start_time", "end_time"):
        _, rate = infer_time_formats(list(values))
        if rate == 0.0 and all(is_all_day(v) for v in values):
            return 0.0, ["all-day marker only"]
        return rate, ["values parse under an inferred time format"]
    if role == "image":
        return _rate(values, _image_is_usable), ["values look like an image URL"]
    if role == "venue":
        return (
            _rate(values, lambda v: 1 <= len(normalize_whitespace(v)) <= 160),
            ["short place text"],
        )
    if role == "address":
        return _rate(values, lambda v: len(normalize_whitespace(v)) >= 6), ["address-length text"]
    if role == "description":
        return _rate(values, lambda v: len(normalize_whitespace(v)) >= 20), ["prose-length text"]
    if role == "source_category":
        return (
            _rate(values, lambda v: 1 <= len(normalize_whitespace(v)) <= 80),
            ["short label text"],
        )
    if role == "external_source_id":
        return _rate(values, lambda v: 0 < len(normalize_whitespace(v)) <= 80), ["short id text"]
    return 1.0, []


def _attribute_allowed(role: str, attribute: str | None) -> bool:
    if role in ("canonical_url", "detail_link"):
        return attribute in _URL_ATTRIBUTES
    if role == "image":
        return attribute in _IMAGE_ATTRIBUTES
    if role in ("start_datetime", "end_datetime"):
        return attribute in _DATE_ATTRIBUTES
    return attribute is None


# A link named for the event's title is the event's own link. A link named
# "details"/"more" is *a* link on the card and only wins when no title link
# exists — this is what keeps a broad anchor from beating the specific one.
_TITLE_LINK_TOKENS: frozenset[str] = frozenset({"title", "name", "headline"})


def _semantic_score(role: str, hints: frozenset[str]) -> float:
    if hints & ROLE_ANTI_VOCAB.get(role, frozenset()):
        return -1.0  # sentinel: disqualified
    vocab = ROLE_VOCAB.get(role, frozenset())
    if role in ("canonical_url", "detail_link"):
        if hints & _TITLE_LINK_TOKENS:
            return 1.0
        return 0.7 if hints & vocab else 0.0
    return 1.0 if hints & vocab else 0.0


def _tag_score(role: str, observation: _Observation) -> float:
    priors = ROLE_TAG_PRIOR.get(role, {})
    best = max((priors.get(tag, _DEFAULT_TAG_PRIOR) for tag in observation.tags), default=0.0)
    if role == "title" and observation.first_anchor:
        best = max(best, 0.6)
    return best


def _schema_score(role: str, observation: _Observation) -> float:
    if not observation.itemprops:
        return 0.4  # no microdata on the page at all — neutral, not negative
    for prop in observation.itemprops:
        if ITEMPROP_ROLES.get(prop.lower()) == role:
            return 1.0
    return 0.0


def _score_role(
    role: str, observation: _Observation, *, base_url: str, policy: AutoOnboardingPolicy
) -> FieldSelectorCandidate | None:
    if not _attribute_allowed(role, observation.attribute):
        return None

    semantic = _semantic_score(role, observation.hints)
    if semantic < 0:
        return None

    values = observation.nonempty
    coverage = observation.coverage
    unique = observation.unique_match_rate
    variation = observation.variation
    parse, parse_evidence = _parse_success(role, values, base_url)
    tag_score = _tag_score(role, observation)
    schema = _schema_score(role, observation)

    warnings: list[str] = []
    if coverage < policy.min_field_coverage:
        return None
    if unique < policy.min_unique_match_rate:
        return None
    if parse < policy.min_parse_success:
        return None
    if role in IDENTITY_ROLES and variation < policy.min_variation_for_identity_fields:
        return None
    # No naming evidence, no schema evidence, and an unremarkable element:
    # nothing here distinguishes this selector from any other text node.
    if semantic == 0.0 and schema < 1.0 and tag_score < 0.6:
        return None
    if observation.selector == "a" and role in ("canonical_url", "detail_link"):
        warnings.append("broad_anchor_selector")

    variation_component = variation if role in IDENTITY_ROLES else 1.0
    stability = selector_stability(observation.selector, policy)
    # A bare element selector (`h3`, `img`) is one page edit away from
    # matching something unrelated; the class-qualified form of the same
    # element is preferred whenever both score alike.
    bare_tag = bool(_BARE_TAG_RE.fullmatch(observation.selector))
    confidence = (
        _W_SEMANTIC * semantic
        + _W_TAG * tag_score
        + _W_SCHEMA * schema
        + _W_COVERAGE * coverage
        + _W_UNIQUE * unique
        + _W_PARSE * parse
        + _W_VARIATION * variation_component
        - _W_STABILITY_PENALTY * (1.0 - stability)
        - (_BARE_TAG_PENALTY if bare_tag else 0.0)
    )
    if warnings:
        confidence -= 0.15
    confidence = max(0.0, min(1.0, confidence))

    evidence: list[str] = []
    if semantic:
        matched = sorted(observation.hints & ROLE_VOCAB.get(role, frozenset()))
        evidence.append(f"semantic name tokens: {', '.join(matched)}")
    if schema == 1.0:
        evidence.append(f"schema.org itemprop agreement: {sorted(observation.itemprops)}")
    evidence.append(f"element type {sorted(observation.tags)} (prior {tag_score:.2f})")
    evidence.append(f"coverage {coverage:.2f}, one-match-per-card {unique:.2f}")
    evidence.append(f"variation between cards {variation:.2f}")
    evidence.extend(parse_evidence)
    evidence.append(f"selector stability {stability:.2f}")
    if bare_tag:
        evidence.append("bare element selector — class-qualified form preferred when tied")

    return FieldSelectorCandidate(
        field=role,
        kind="css",
        selector=observation.selector,
        attribute=observation.attribute,
        confidence=confidence,
        coverage=coverage,
        parse_success_rate=parse,
        evidence=tuple(evidence),
        sample_values=tuple(values[: policy.max_sample_values_recorded]),
        warnings=tuple(warnings),
        alternatives=(),
        accepted=False,
    )


def _required_role(role: str) -> bool:
    return role in ("title", "canonical_url", "start_datetime")


def infer_fields(
    cards: list[Tag],
    *,
    base_url: str,
    policy: AutoOnboardingPolicy,
    roles: tuple[str, ...] = ROLES,
) -> tuple[dict[str, FieldSelectorCandidate], tuple[FieldSelectorCandidate, ...]]:
    """Returns (accepted field -> candidate, every scored candidate).

    The second element includes rejected candidates — the admin UI shows them
    as "alternatives considered", and a rejected *required* field is exactly
    what routes a source to needs_review instead of ready_for_approval.
    """
    collected = _collect_candidates(cards, policy)
    observations = _observe(cards, collected)

    accepted: dict[str, FieldSelectorCandidate] = {}
    reported: list[FieldSelectorCandidate] = []

    for role in roles:
        scored = [
            candidate
            for candidate in (
                _score_role(role, observation, base_url=base_url, policy=policy)
                for observation in observations
            )
            if candidate is not None
        ]
        if not scored:
            continue
        # Deterministic order: confidence, then the simpler/shorter selector.
        scored.sort(key=lambda c: (-c.confidence, len(c.selector or ""), c.selector or ""))
        best = scored[0]
        threshold = (
            policy.min_required_field_confidence
            if _required_role(role)
            else policy.min_field_confidence
        )
        alternatives = tuple(
            f"{c.selector}{'@' + c.attribute if c.attribute else ''} ({c.confidence:.2f})"
            for c in scored[1 : 1 + policy.max_alternatives_recorded]
        )
        is_accepted = best.confidence >= threshold
        resolved = FieldSelectorCandidate(
            field=best.field,
            kind=best.kind,
            selector=best.selector,
            attribute=best.attribute,
            confidence=best.confidence,
            coverage=best.coverage,
            parse_success_rate=best.parse_success_rate,
            evidence=best.evidence,
            sample_values=best.sample_values,
            warnings=best.warnings
            if is_accepted
            else (*best.warnings, f"below_confidence_threshold:{threshold:.2f}"),
            alternatives=alternatives,
            accepted=is_accepted,
        )
        reported.append(resolved)
        if is_accepted:
            accepted[role] = resolved

    return accepted, tuple(reported)


def sample_cards(soup: BeautifulSoup, selector: str, policy: AutoOnboardingPolicy) -> list[Tag]:
    return list(soup.select(selector))[: policy.max_sample_cards]


__all__ = [
    "IDENTITY_ROLES",
    "ROLES",
    "ContainerCandidate",
    "infer_container",
    "infer_fields",
    "is_stable_class_token",
    "sample_cards",
]
