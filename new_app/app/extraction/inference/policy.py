"""AutoOnboardingPolicy: every threshold, cap and bound the automatic
configuration-inference path is allowed to use, in one place.

Nothing in app.extraction.inference hard-codes a numeric threshold inline —
they all live here so a deployment can tune automation aggressiveness (or a
test can tighten/loosen a single gate) without touching inference logic.
"""

from __future__ import annotations

from dataclasses import dataclass

# Onboarding outcome states. `ready_for_approval` is only ever reached after
# a preview has actually run and been scored (see
# app.services.onboarding_automation); the inference service uses it as a
# provisional value meaning "nothing in the proposal itself blocks approval".
READY_FOR_APPROVAL = "ready_for_approval"
NEEDS_REVIEW = "needs_review"
UNSUPPORTED = "unsupported"
BROWSER_REQUIRED = "browser_required"
BLOCKED = "blocked"
FAILED = "failed"

ONBOARDING_OUTCOMES: tuple[str, ...] = (
    READY_FOR_APPROVAL,
    NEEDS_REVIEW,
    UNSUPPORTED,
    BROWSER_REQUIRED,
    BLOCKED,
    FAILED,
)


@dataclass(frozen=True)
class AutoOnboardingPolicy:
    """Frozen so a policy can be safely shared across an inference run."""

    # --- Detection -------------------------------------------------------
    min_pattern_confidence: float = 0.6

    # --- Generic HTML card sampling --------------------------------------
    # Bounded inspection: at most this many repeated cards are ever examined,
    # regardless of how many the page contains.
    max_sample_cards: int = 12
    min_cards_for_inference: int = 3
    max_candidate_selectors_per_card: int = 120
    max_sample_values_recorded: int = 3

    # --- Field candidate acceptance --------------------------------------
    # A candidate below `min_field_confidence` is recorded as evidence but
    # never written into the proposed configuration. Fields that participate
    # in the minimal required set must additionally clear the (higher)
    # required threshold — "do not populate a required field when confidence
    # is below policy threshold".
    min_field_confidence: float = 0.45
    min_required_field_confidence: float = 0.55
    min_field_coverage: float = 0.5
    max_alternatives_recorded: int = 3
    # A selector that resolves to more than one element inside a card can't
    # produce a stable single value, and a selector whose values never differ
    # between cards is measuring the page chrome rather than the event.
    min_unique_match_rate: float = 0.6
    min_variation_for_identity_fields: float = 0.5
    min_parse_success: float = 0.5

    # --- Selector shape ---------------------------------------------------
    # "Avoid very deep selector chains": a chain is counted in combinator-
    # separated parts, so `h3.title a` is 2 and `div.a > div.b span` is 3.
    max_selector_parts: int = 3
    max_selector_length: int = 120

    # --- Date/time format inference --------------------------------------
    min_date_format_match_rate: float = 0.6
    max_date_formats_proposed: int = 4

    # --- Proposed configuration bounds -----------------------------------
    max_pages: int = 5
    max_events: int = 250
    # Detail-page enrichment is only ever proposed when the listing card
    # cannot yield a full date on its own; this caps it when it is.
    max_detail_fetches_when_needed: int = 10
    max_detail_fetches_default: int = 0
    structured_page_size: int = 50

    # --- Preview quality gates (ready_for_approval) ----------------------
    min_valid_candidates: int = 3
    min_valid_percentage: float = 0.6
    min_date_parse_success: float = 0.6
    min_url_validity: float = 0.8
    max_duplicate_rate: float = 0.5


DEFAULT_POLICY = AutoOnboardingPolicy()
