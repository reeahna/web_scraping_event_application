"""Shared geographic filter — post-normalization, pre-persistence.

One service every pattern runs a normalized candidate through to decide whether
the event belongs to the source's coverage area. It is deterministic and
side-effect free, and it decides inclusion purely from the event's own
geography (address text, venue, coordinates) — never from the city the event
was assigned to, which is not evidence of where the event actually is.

It returns structured evidence and provenance (which rule groups were checked,
which matched, and what values were examined) so an approval decision and an
admin review can both see exactly why an event was kept or dropped. No fuzzy
matching (unless a config explicitly opts in, which this phase never does) and
no geocoding: radius/bounding-box rules apply only when coordinates are already
present.
"""

from __future__ import annotations

import dataclasses
import math
import re
from dataclasses import dataclass, field

from app.extraction.types import EventCandidate
from app.schemas.geographic import GeographicFilterConfig

Outcome = str  # "included" | "excluded" | "missing_reject" | "missing_keep" | "missing_review"

# Provenance token recorded on a candidate's transformation_history (NOT its
# warnings, so it never inflates the warning count that policy gates on). Both
# preview quality and persistence read it, so the geo decision is made exactly
# once, in the shared pipeline.
GEO_HISTORY_PREFIX = "geographic:"
_DROP_OUTCOMES = frozenset({"excluded", "missing_reject"})
_REVIEW_OUTCOMES = frozenset({"missing_review"})


def annotate_candidate_geography(
    candidate: EventCandidate, config: GeographicFilterConfig | None
) -> EventCandidate:
    """Stamp the geographic decision onto the candidate's provenance history.
    A no-op when no filter is configured, so default behaviour is unchanged."""
    if config is None or not config.has_any_rule():
        return candidate
    decision = apply_geographic_filter(candidate, config)
    return dataclasses.replace(
        candidate,
        transformation_history=(
            *candidate.transformation_history,
            f"{GEO_HISTORY_PREFIX}{decision.outcome}",
        ),
    )


def geo_outcome_of(candidate: EventCandidate) -> str | None:
    for token in reversed(candidate.transformation_history):
        if token.startswith(GEO_HISTORY_PREFIX):
            return token[len(GEO_HISTORY_PREFIX):]
    return None


def geo_should_drop(candidate: EventCandidate) -> bool:
    return geo_outcome_of(candidate) in _DROP_OUTCOMES


def geo_needs_review(candidate: EventCandidate) -> bool:
    return geo_outcome_of(candidate) in _REVIEW_OUTCOMES


@dataclass(frozen=True)
class GeoFilterDecision:
    included: bool
    outcome: Outcome
    geography_missing: bool
    matched_rules: tuple[str, ...] = ()
    checked_rules: tuple[str, ...] = ()
    provenance: dict = field(default_factory=dict)
    warning: str | None = None

    @property
    def needs_review(self) -> bool:
        return self.outcome == "missing_review"


def _text_blob(candidate: EventCandidate) -> str:
    parts = [candidate.address or "", candidate.venue or ""]
    return " ".join(p for p in parts if p).lower()


def _word_boundary_match(term: str, blob: str) -> bool:
    term = term.strip().lower()
    if not term:
        return False
    return re.search(rf"(?<![\w]){re.escape(term)}(?![\w])", blob) is not None


def _expand_with_aliases(terms: list[str], aliases: dict[str, list[str]]) -> list[str]:
    expanded: list[str] = []
    for term in terms:
        expanded.append(term)
        for variant in aliases.get(term, []):
            expanded.append(variant)
    return expanded


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * radius * math.asin(min(1.0, math.sqrt(a)))


def apply_geographic_filter(
    candidate: EventCandidate, config: GeographicFilterConfig | None
) -> GeoFilterDecision:
    if config is None or not config.has_any_rule():
        return GeoFilterDecision(
            included=True, outcome="included", geography_missing=False,
            provenance={"reason": "no_filter_configured"},
        )

    blob = _text_blob(candidate)
    has_text = bool(blob.strip())
    has_coords = candidate.latitude is not None and candidate.longitude is not None

    if not has_text and not has_coords:
        return _missing(config)

    matched: list[str] = []
    checked: list[str] = []

    def check_names(group_name: str, terms: list[str]) -> bool | None:
        if not terms:
            return None
        checked.append(group_name)
        candidates = _expand_with_aliases(terms, config.aliases)
        if any(_word_boundary_match(t, blob) for t in candidates):
            matched.append(group_name)
            return True
        return False

    results = [
        check_names("localities", config.localities),
        check_names("regions", config.regions),
        check_names("countries", config.countries),
        _check_postal(config, blob, matched, checked),
        _check_address_contains(config, blob, matched, checked),
        _check_radius(config, candidate, has_coords, matched, checked),
        _check_bounding_box(config, candidate, has_coords, matched, checked),
    ]
    evaluated = [r for r in results if r is not None]

    if not evaluated:
        # Rules exist but none could be evaluated against this candidate's
        # geography (e.g. only radius configured but no coordinates present).
        return _missing(config)

    included = all(evaluated) if config.mode == "all" else any(evaluated)
    provenance = {
        "mode": config.mode,
        "examined_text": blob[:500],
        "had_coordinates": has_coords,
    }
    return GeoFilterDecision(
        included=included,
        outcome="included" if included else "excluded",
        geography_missing=False,
        matched_rules=tuple(matched),
        checked_rules=tuple(checked),
        provenance=provenance,
    )


def _missing(config: GeographicFilterConfig) -> GeoFilterDecision:
    action = config.missing_geography_action
    if action == "reject":
        return GeoFilterDecision(
            included=False, outcome="missing_reject", geography_missing=True,
            warning="geography_missing_rejected",
            provenance={"missing_geography_action": action},
        )
    if action == "needs_review":
        return GeoFilterDecision(
            included=True, outcome="missing_review", geography_missing=True,
            warning="geography_missing_needs_review",
            provenance={"missing_geography_action": action},
        )
    return GeoFilterDecision(
        included=True, outcome="missing_keep", geography_missing=True,
        warning="geography_missing_kept",
        provenance={"missing_geography_action": action},
    )


def _check_postal(config, blob, matched, checked) -> bool | None:
    if not config.postal_codes and not config.postal_code_prefixes:
        return None
    checked.append("postal")
    for code in config.postal_codes:
        if _word_boundary_match(code, blob):
            matched.append("postal")
            return True
    tokens = re.findall(r"[a-z0-9\-]+", blob)
    for prefix in config.postal_code_prefixes:
        pref = prefix.strip().lower()
        if pref and any(tok.startswith(pref) for tok in tokens):
            matched.append("postal")
            return True
    return False


def _check_address_contains(config, blob, matched, checked) -> bool | None:
    if not config.address_contains:
        return None
    checked.append("address_contains")
    if any(term.strip().lower() in blob for term in config.address_contains if term.strip()):
        matched.append("address_contains")
        return True
    return False


def _check_radius(config, candidate, has_coords, matched, checked) -> bool | None:
    if config.radius is None:
        return None
    if not has_coords:
        return None  # never geocode; unevaluable without coordinates
    checked.append("radius")
    distance = _haversine_km(
        candidate.latitude, candidate.longitude,
        config.radius.center_latitude, config.radius.center_longitude,
    )
    if distance <= config.radius.radius_km:
        matched.append("radius")
        return True
    return False


def _check_bounding_box(config, candidate, has_coords, matched, checked) -> bool | None:
    if config.bounding_box is None:
        return None
    if not has_coords:
        return None
    checked.append("bounding_box")
    box = config.bounding_box
    if (
        box.min_latitude <= candidate.latitude <= box.max_latitude
        and box.min_longitude <= candidate.longitude <= box.max_longitude
    ):
        matched.append("bounding_box")
        return True
    return False
