"""Configuration proposer for `generic_html_cards`.

This is the pattern with nothing to fall back on: there is no API schema and
no microdata contract, so the container, every field selector, every
attribute, every transformation and every date/time format has to be inferred
from the markup itself. All of that inference is deterministic evidence
scoring (see html_fields.py and dates.py) — no LLM, no heuristic keyed to a
hostname, no positional selectors.

Three behaviours are worth calling out because they are what stops the
proposer from inventing data:

* A year is never fabricated. When the card's own date text carries no
  four-digit year, the proposer asks the caller for exactly one bounded
  detail-page document (`detail_probe_url`) and infers the date from there
  instead — through the same SSRF-safe fetch path everything else uses.
* A date range is only split when *both* halves are complete, explicit
  dates (`dates.split_explicit_range`).
* A required field whose best candidate scores below policy threshold is
  simply left out, which is what makes the source `needs_review` rather
  than silently mis-extracted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from app.extraction.inference.base import DEFAULT_REQUIRED_FIELDS, failed_proposal
from app.extraction.inference.dates import (
    DATE_FORMAT_TABLE,
    DATE_SUBSTRING_PATTERNS,
    TIME_FORMAT_TABLE,
    TIME_SUBSTRING_PATTERN,
    infer_date_formats,
    infer_extraction_pattern,
    infer_time_formats,
    normalize_whitespace,
    split_explicit_range,
)
from app.extraction.inference.html_fields import (
    infer_container,
    infer_fields,
    sample_cards,
)
from app.extraction.inference.policy import AutoOnboardingPolicy
from app.extraction.inference.types import (
    ConfigurationProposal,
    DateFormatCandidate,
    FieldSelectorCandidate,
    ProposalContext,
)
from app.extraction.selectors import resolve_css
from app.schemas.extraction import (
    FieldSelectorConfig,
    SiteConfiguration,
    TransformationRuleConfig,
)

NAME = "generic_html_cards"

# Range separators, tried in order. Written without non-capturing groups
# because app.core.safe_regex forbids them in any stored transformation.
_RANGE_PATTERNS: tuple[str, ...] = (
    r"^(.+?)\s*[-–—]\s*(.+)$",
    r"^(.+?)\s+to\s+(.+)$",
)
_CSS_URL_PATTERN = r"url\(['\"]?([^'\"\)]+)"
_SRCSET_FIRST_URL_PATTERN = r"^\s*([^\s,]+)"
_AT_VENUE_PATTERN = r"@\s*(.+)$"
_TIME_BLOB_RE = re.compile(r"[:\d]")

# Anchors that advance to the next listing page. `rel="next"` is the
# standards-defined signal; a `next`-named class is the common substitute.
_NEXT_LINK_SELECTORS: tuple[str, ...] = (
    'a[rel~="next"]',
    "a.next",
    "a.next-page",
    "a.pagination-next",
)


@dataclass
class _DateOutcome:
    formats: list[str]
    candidates: list[DateFormatCandidate]
    transformations: list[TransformationRuleConfig]
    end_transformation: TransformationRuleConfig | None
    match_rate: float
    warnings: list[str]


def _values(cards: list[Tag], selector: str, attribute: str | None) -> list[str | None]:
    out: list[str | None] = []
    for card in cards:
        value = resolve_css(card, selector, attribute).value
        if isinstance(value, list):
            value = " ".join(str(v) for v in value)
        out.append(str(value).strip() if value is not None and str(value).strip() else None)
    return out


def _needs_collapse(values: list[str | None]) -> bool:
    return any(v is not None and v != normalize_whitespace(v) for v in values)


def _collapse_rule(field_name: str) -> TransformationRuleConfig:
    return TransformationRuleConfig(field=field_name, kind="collapse_whitespace")


def _regex_rule(field_name: str, pattern: str, group: int = 1) -> TransformationRuleConfig:
    return TransformationRuleConfig(
        field=field_name,
        kind="regex_extract_group",
        params={"pattern": pattern, "group": group},
    )


def _extracted(values: list[str], pattern: str, group: int = 1) -> list[str]:
    compiled = re.compile(pattern)
    out: list[str] = []
    for value in values:
        match = compiled.search(value)
        if match:
            out.append(match.group(group))
    return out


def _infer_date_configuration(
    raw_values: list[str | None], *, field_name: str, policy: AutoOnboardingPolicy
) -> _DateOutcome:
    transformations: list[TransformationRuleConfig] = []
    warnings: list[str] = []
    if _needs_collapse(raw_values):
        transformations.append(_collapse_rule(field_name))
    normalized = [normalize_whitespace(v) for v in raw_values if v]

    candidates, rate = infer_date_formats(normalized, max_formats=policy.max_date_formats_proposed)
    if rate >= policy.min_date_format_match_rate:
        return _DateOutcome(
            formats=[c.format for c in candidates if c.accepted and c.format],
            candidates=candidates,
            transformations=transformations,
            end_transformation=None,
            match_rate=rate,
            warnings=warnings,
        )

    # An explicit two-sided range: keep the start, and offer the end as its
    # own field rather than discarding half the information. A range with an
    # implicit endpoint ("Sep 12 - 13, 2026") fails this on its own, because
    # the extracted start "Sep 12" carries no year and so parses under no
    # format — no special case is needed to exclude it.
    for pattern in _RANGE_PATTERNS:
        starts = _extracted(normalized, pattern, group=1)
        if len(starts) != len(normalized):
            continue
        range_candidates, range_rate = infer_date_formats(
            starts, max_formats=policy.max_date_formats_proposed
        )
        if range_rate < policy.min_date_format_match_rate:
            continue
        transformations.append(_regex_rule(field_name, pattern, group=1))
        both_halves_explicit = all(split_explicit_range(v) is not None for v in normalized)
        return _DateOutcome(
            formats=[c.format for c in range_candidates if c.accepted and c.format],
            candidates=range_candidates,
            transformations=transformations,
            end_transformation=(
                _regex_rule("end_datetime", pattern, group=2) if both_halves_explicit else None
            ),
            match_rate=range_rate,
            warnings=[*warnings, "explicit_date_range_split"],
        )

    # A date embedded in a longer blob ("Fri Jul 24, 2026 + Add to calendar").
    best = infer_extraction_pattern(normalized, DATE_SUBSTRING_PATTERNS, DATE_FORMAT_TABLE)
    if best is not None and best[1] >= policy.min_date_format_match_rate:
        pattern, _ = best
        extracted = _extracted(normalized, pattern)
        sub_candidates, sub_rate = infer_date_formats(
            extracted, max_formats=policy.max_date_formats_proposed
        )
        transformations.append(_regex_rule(field_name, pattern))
        return _DateOutcome(
            formats=[c.format for c in sub_candidates if c.accepted and c.format],
            candidates=sub_candidates,
            transformations=transformations,
            end_transformation=None,
            match_rate=sub_rate,
            warnings=[*warnings, "date_extracted_from_surrounding_text"],
        )

    for candidate in candidates:
        warnings.extend(candidate.warnings)
    return _DateOutcome(
        formats=[],
        candidates=candidates,
        transformations=[],
        end_transformation=None,
        match_rate=rate,
        warnings=warnings,
    )


def _infer_time_configuration(
    raw_values: list[str | None], *, field_name: str, policy: AutoOnboardingPolicy
) -> tuple[list[str], list[DateFormatCandidate], list[TransformationRuleConfig]]:
    transformations: list[TransformationRuleConfig] = []
    if _needs_collapse(raw_values):
        transformations.append(_collapse_rule(field_name))
    normalized = [normalize_whitespace(v) for v in raw_values if v]

    candidates, rate = infer_time_formats(normalized)
    if rate >= policy.min_date_format_match_rate:
        formats = [c.format for c in candidates if c.accepted and c.format]
        return formats, candidates, transformations

    best = infer_extraction_pattern(normalized, (TIME_SUBSTRING_PATTERN,), TIME_FORMAT_TABLE)
    if best is not None and best[1] >= policy.min_date_format_match_rate:
        extracted = _extracted(normalized, TIME_SUBSTRING_PATTERN)
        sub_candidates, _ = infer_time_formats(extracted)
        transformations.append(_regex_rule(field_name, TIME_SUBSTRING_PATTERN))
        return (
            [c.format for c in sub_candidates if c.accepted and c.format],
            sub_candidates,
            transformations,
        )
    return [], candidates, []


def _infer_image_transformations(
    candidate: FieldSelectorCandidate, values: list[str | None]
) -> list[TransformationRuleConfig]:
    sample = [v for v in values if v]
    if not sample:
        return []
    if candidate.attribute == "style" and all("url(" in v for v in sample):
        return [_regex_rule("image", _CSS_URL_PATTERN)]
    if candidate.attribute in ("srcset", "data-srcset"):
        return [_regex_rule("image", _SRCSET_FIRST_URL_PATTERN)]
    return []


def _infer_venue_transformations(values: list[str | None]) -> list[TransformationRuleConfig]:
    """A venue rendered as the tail of a "…times… @ Place" blob. Gated on the
    text *before* the separator actually looking like schedule noise, so a
    plain venue name containing an "@" is left alone."""
    sample = [normalize_whitespace(v) for v in values if v]
    if len(sample) < 2 or not all("@" in v for v in sample):
        return []
    prefixes = [v.split("@", 1)[0] for v in sample]
    if not all(_TIME_BLOB_RE.search(p) for p in prefixes):
        return []
    if not all(v.split("@", 1)[1].strip() for v in sample):
        return []
    return [_regex_rule("venue", _AT_VENUE_PATTERN)]


def _infer_pagination(soup: BeautifulSoup) -> tuple[str, str | None, list[str]]:
    for selector in _NEXT_LINK_SELECTORS:
        link = soup.select_one(selector)
        if link is not None and link.get("href"):
            return "next_link", selector, [f"next-page link found via {selector}"]
    return "none", None, ["no next-page link found — single-page listing"]


def _first_absolute(values: list[str | None], base_url: str) -> str | None:
    for value in values:
        if value:
            return urljoin(base_url, value)
    return None


class GenericHtmlCardsProposer:
    pattern_name = NAME

    def propose(self, context: ProposalContext) -> ConfigurationProposal:
        policy = context.policy
        base_url = context.response.final_url
        soup = BeautifulSoup(context.response.text, "html.parser")

        container = infer_container(soup, policy)
        if container is None:
            return failed_proposal(
                "no repeated event-card structure could be identified on this page"
            )

        cards = sample_cards(soup, container.selector, policy)
        if len(cards) < policy.min_cards_for_inference:
            return failed_proposal(
                f"only {len(cards)} repeated cards found "
                f"(minimum {policy.min_cards_for_inference})"
            )

        accepted, reported = infer_fields(cards, base_url=base_url, policy=policy)

        field_selectors: dict[str, FieldSelectorConfig] = {}
        transformations: list[TransformationRuleConfig] = []
        date_candidates: list[DateFormatCandidate] = []
        warnings: list[str] = []
        notes: list[str] = [
            f"container: {container.selector} ({container.count} cards, "
            f"confidence {container.confidence:.2f})",
            *container.evidence,
        ]

        for role, candidate in accepted.items():
            if candidate.selector is None:
                continue
            field_selectors[role] = FieldSelectorConfig(
                kind="css", selector=candidate.selector, attribute=candidate.attribute
            )

        # --- dates -----------------------------------------------------
        date_formats: list[str] = []
        date_rate = 0.0
        start_candidate = accepted.get("start_datetime")
        if start_candidate is not None and start_candidate.selector:
            raw = _values(cards, start_candidate.selector, start_candidate.attribute)
            outcome = _infer_date_configuration(raw, field_name="start_datetime", policy=policy)
            date_formats = outcome.formats
            date_rate = outcome.match_rate
            date_candidates.extend(outcome.candidates)
            transformations.extend(outcome.transformations)
            warnings.extend(outcome.warnings)
            if outcome.end_transformation is not None:
                field_selectors["end_datetime"] = FieldSelectorConfig(
                    kind="css",
                    selector=start_candidate.selector,
                    attribute=start_candidate.attribute,
                )
                transformations.append(outcome.end_transformation)

        # --- times -----------------------------------------------------
        time_formats: list[str] = []
        time_candidate = accepted.get("start_time")
        if time_candidate is not None and time_candidate.selector:
            raw = _values(cards, time_candidate.selector, time_candidate.attribute)
            time_formats, time_candidates, time_transformations = _infer_time_configuration(
                raw, field_name="start_time", policy=policy
            )
            date_candidates.extend(time_candidates)
            transformations.extend(time_transformations)
            if not time_formats:
                field_selectors.pop("start_time", None)
                warnings.append("start_time_dropped_unparseable")

        # --- image / venue shaping -------------------------------------
        image_candidate = accepted.get("image")
        if image_candidate is not None and image_candidate.selector:
            raw = _values(cards, image_candidate.selector, image_candidate.attribute)
            transformations.extend(_infer_image_transformations(image_candidate, raw))
        venue_candidate = accepted.get("venue")
        if venue_candidate is not None and venue_candidate.selector:
            raw = _values(cards, venue_candidate.selector, venue_candidate.attribute)
            transformations.extend(_infer_venue_transformations(raw))

        # --- bounded detail-page enrichment ----------------------------
        max_detail_fetches = policy.max_detail_fetches_default
        detail_probe_url: str | None = None
        link_candidate = accepted.get("canonical_url")
        date_is_usable = bool(date_formats) and date_rate >= policy.min_date_format_match_rate

        if not date_is_usable and link_candidate is not None and link_candidate.selector:
            link_values = _values(cards, link_candidate.selector, link_candidate.attribute)
            probe_url = _first_absolute(link_values, base_url)
            document = context.detail_documents.get(probe_url or "")
            if probe_url and document is None:
                return ConfigurationProposal(
                    configuration=None,
                    field_candidates=reported,
                    detail_probe_url=probe_url,
                    notes=(
                        *notes,
                        "listing cards carry no complete date — requesting one bounded "
                        "detail-page document before proposing a configuration",
                    ),
                    warnings=tuple(warnings),
                )
            if probe_url and document is not None:
                detail_probe_url = probe_url
                (
                    detail_formats,
                    detail_fields,
                    detail_notes,
                    detail_transformations,
                ) = self._infer_from_detail_page(document, probe_url, policy)
                notes.extend(detail_notes)
                transformations.extend(detail_transformations)
                if detail_fields:
                    field_selectors["detail_link"] = FieldSelectorConfig(
                        kind="css",
                        selector=link_candidate.selector,
                        attribute=link_candidate.attribute,
                    )
                    field_selectors.update(detail_fields)
                    reported = (*reported, *detail_fields_as_candidates(detail_fields))
                    max_detail_fetches = policy.max_detail_fetches_when_needed
                if detail_formats:
                    date_formats = list(dict.fromkeys([*date_formats, *detail_formats]))
                    date_is_usable = True

        # --- assemble ---------------------------------------------------
        missing: list[str] = []
        if "title" not in accepted:
            missing.append("title")
        if "canonical_url" not in accepted:
            missing.append("canonical_url")
        has_date_field = (
            "start_datetime" in field_selectors or "detail_start_datetime" in field_selectors
        )
        if not has_date_field:
            missing.append("start_date")
        elif not date_is_usable:
            missing.append("start_date")
            warnings.append("no_usable_date_format_inferred")

        strategy, next_selector, pagination_notes = _infer_pagination(soup)
        notes.extend(pagination_notes)

        try:
            configuration = SiteConfiguration(
                pattern_name=NAME,
                listing_url=context.listing_url,
                timezone=context.fallback_timezone,
                event_container_selector=container.selector,
                field_selectors=field_selectors,
                date_formats=date_formats,
                time_formats=time_formats,
                transformations=transformations,
                max_detail_fetches=max_detail_fetches,
                pagination={
                    "strategy": strategy,
                    "next_page_selector": next_selector,
                    "max_pages": policy.max_pages,
                    "max_events": policy.max_events,
                },
                required_fields=list(DEFAULT_REQUIRED_FIELDS),
            )
        except ValueError as exc:
            return failed_proposal(f"proposed configuration failed validation: {exc}")

        confidence = _proposal_confidence(container.confidence, accepted, date_is_usable)
        return ConfigurationProposal(
            configuration=configuration,
            field_candidates=reported,
            date_format_candidates=tuple(date_candidates),
            confidence=confidence,
            missing_required_fields=tuple(missing),
            warnings=tuple(dict.fromkeys(warnings)),
            notes=tuple(notes),
            detail_probe_url=detail_probe_url,
        )

    @staticmethod
    def _infer_from_detail_page(
        document: str, url: str, policy: AutoOnboardingPolicy
    ) -> tuple[
        list[str], dict[str, FieldSelectorConfig], list[str], list[TransformationRuleConfig]
    ]:
        """One detail page is a single sample, so cross-card agreement can't
        be measured here. That is recorded as a note rather than papered
        over — the automatic preview across every card is what actually
        confirms the selector generalizes."""
        soup = BeautifulSoup(document, "html.parser")
        root = soup.body or soup
        accepted, _ = infer_fields(
            [root],
            base_url=url,
            policy=policy,
            roles=("start_datetime", "description"),
        )
        fields: dict[str, FieldSelectorConfig] = {}
        formats: list[str] = []
        transformations: list[TransformationRuleConfig] = []
        notes = [f"detail-page probe: {url}", "detail selectors inferred from a single sample"]

        start = accepted.get("start_datetime")
        if start is not None and start.selector:
            raw = _values([root], start.selector, start.attribute)
            outcome = _infer_date_configuration(raw, field_name="start_datetime", policy=policy)
            if outcome.formats:
                fields["detail_start_datetime"] = FieldSelectorConfig(
                    kind="css", selector=start.selector, attribute=start.attribute
                )
                formats = outcome.formats
                # detail_pages.py merges `detail_<field>` back onto `<field>`
                # before normalization, so these rules are keyed to the base
                # field name they will actually be applied to.
                transformations = outcome.transformations
                notes.append(f"detail page supplies a complete date via {start.selector}")

        description = accepted.get("description")
        if description is not None and description.selector and fields:
            fields["detail_description"] = FieldSelectorConfig(
                kind="css", selector=description.selector, attribute=description.attribute
            )
        return formats, fields, notes, transformations


def detail_fields_as_candidates(
    fields: dict[str, FieldSelectorConfig],
) -> tuple[FieldSelectorCandidate, ...]:
    return tuple(
        FieldSelectorCandidate(
            field=name,
            kind="css",
            selector=config.selector,
            attribute=config.attribute,
            confidence=1.0,
            coverage=1.0,
            parse_success_rate=1.0,
            evidence=("inferred from the bounded detail-page probe",),
            sample_values=(),
            warnings=("single_detail_page_sample",),
            alternatives=(),
            accepted=True,
        )
        for name, config in sorted(fields.items())
    )


def _proposal_confidence(
    container_confidence: float,
    accepted: dict[str, FieldSelectorCandidate],
    date_is_usable: bool,
) -> float:
    required = ["title", "canonical_url"]
    field_scores = [accepted[r].confidence for r in required if r in accepted]
    if not field_scores or not date_is_usable:
        return min(container_confidence, 0.5)
    field_mean = sum(field_scores) / len(field_scores)
    return round(min(0.95, 0.4 * container_confidence + 0.6 * field_mean), 4)
