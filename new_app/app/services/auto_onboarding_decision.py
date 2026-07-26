"""AutoOnboardingDecisionService: does this source qualify for automatic
approval, and separately, for automatic activation?

**Pure.** `evaluate()` takes an immutable context and returns an immutable
result. It opens no Session, writes nothing, transitions nothing and fetches
nothing — so every qualification rule is directly testable, and a decision is
reproducible from the snapshot it recorded.

Two structural rules make the outcome trustworthy:

* **Fail closed.** A source qualifies only when *every* required condition
  passes. Anything unknown — no policy, no preview, an unrecognized pattern,
  a missing metric — is a failure, never a pass.
* **Every rule reports both ways.** A rule that passes appends to
  `reasons_passed`; a rule that fails appends to `reasons_failed` and does not
  stop the evaluation. An administrator seeing "denied" gets the full list of
  what failed, not just the first thing.

`evaluate()` never returns "automatically approved" — approving is an action,
and actions live in the execution service. The most this can say is that
approval is *allowed*.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.core.auto_onboarding import (
    AUTOMATIC_APPROVAL_ALLOWED,
    AUTOMATIC_APPROVAL_DENIED,
    NO_APPLICABLE_POLICY,
    ORIGIN_ADMINISTRATOR_MANUAL,
    ORIGIN_AI_SUGGESTED,
    ORIGIN_DETERMINISTIC_GENERIC_HTML,
    ORIGIN_DETERMINISTIC_STRUCTURED,
    ORIGIN_IMPORTED,
    POLICY_DISABLED,
)
from app.core.url_safety import UnsafeURLError, validate_public_url
from app.extraction.inference.selectors import is_stable_class_token, selector_parts
from app.extraction.inference.types import PreviewQualityResult
from app.schemas.extraction import SiteConfiguration

GENERIC_HTML_PATTERN = "generic_html_cards"

# Required fields, in the *typed* vocabulary the validator and preview quality
# both speak (see the alias contract test in test_onboarding_corrections).
REQUIRED_TYPED_FIELDS: tuple[str, ...] = ("title", "start_date", "canonical_url")
# ...and the raw selector keys those correspond to for generic_html_cards.
REQUIRED_SELECTOR_FIELDS: tuple[str, ...] = ("title", "canonical_url", "start_datetime")

# Warning codes that indicate a safety or fetch-integrity problem rather than
# a data-quality one. Deliberately a short closed list: a warning has to be
# *known* to be serious to count as critical.
CRITICAL_WARNING_MARKERS: tuple[str, ...] = (
    "ssrf_blocked",
    "blocked",
    "detail_page_fetch_failed",
    "unexpected_content_type",
    "too_many_redirects",
    "failed:",
)

# A configuration this application derived itself, deterministically.
DETERMINISTIC_ORIGINS: frozenset[str] = frozenset(
    {ORIGIN_DETERMINISTIC_STRUCTURED, ORIGIN_DETERMINISTIC_GENERIC_HTML}
)

_POSITIONAL_SELECTOR_RE = re.compile(r":nth-(child|of-type)", re.IGNORECASE)
_ACCEPTABLE_PREVIEW_STATUSES: frozenset[str] = frozenset({"success", "partial"})
_BROAD_ANCHOR_SELECTORS: frozenset[str] = frozenset({"a", "a[href]", "*"})


# --- Immutable inputs -------------------------------------------------------


@dataclass(frozen=True)
class PolicySnapshot:
    """An immutable copy of the policy as it stood when the decision was made.

    Taken as a snapshot rather than held as an ORM row so that a policy edit
    mid-evaluation cannot change the rules underneath a decision, and so the
    decision service cannot accidentally lazy-load anything.
    """

    policy_id: int
    name: str
    version: int
    active: bool

    automatic_configuration_enabled: bool
    automatic_preview_enabled: bool
    automatic_approval_enabled: bool
    automatic_activation_enabled: bool

    allowed_pattern_names: frozenset[str]
    allow_generic_html_cards: bool
    allow_browser_required: bool
    allow_ai_origin: bool
    allow_administrator_manual_origin: bool
    allow_imported_configuration: bool
    allow_detail_page_enrichment: bool

    minimum_detector_confidence: float
    minimum_events_found: int
    minimum_valid_events: int
    minimum_valid_percentage: float
    maximum_rejected_percentage: float
    minimum_canonical_url_coverage: float
    minimum_start_date_coverage: float
    minimum_start_date_parse_success: float
    maximum_duplicate_rate: float
    maximum_warning_count: int
    maximum_critical_warning_count: int
    minimum_distinct_events: int
    require_absolute_public_urls: bool
    require_zero_critical_warnings: bool
    require_distinct_events: bool

    generic_html_minimum_detector_confidence: float
    generic_html_minimum_events_found: int
    generic_html_minimum_valid_events: int
    generic_html_minimum_valid_percentage: float
    generic_html_maximum_rejected_percentage: float
    generic_html_minimum_required_field_confidence: float
    generic_html_minimum_required_field_coverage: float
    generic_html_minimum_date_format_confidence: float
    generic_html_minimum_distinct_event_count: int
    generic_html_reject_broad_canonical_selector: bool
    generic_html_reject_unstable_required_selectors: bool

    city_ids: frozenset[int] = frozenset()
    role_ids: frozenset[int] = frozenset()


def snapshot_policy(policy, *, city_ids=(), role_ids=()) -> PolicySnapshot:
    """Builds a PolicySnapshot from an AutoOnboardingPolicy ORM row."""
    return PolicySnapshot(
        policy_id=policy.id,
        name=policy.name,
        version=policy.version,
        active=policy.active,
        automatic_configuration_enabled=policy.automatic_configuration_enabled,
        automatic_preview_enabled=policy.automatic_preview_enabled,
        automatic_approval_enabled=policy.automatic_approval_enabled,
        automatic_activation_enabled=policy.automatic_activation_enabled,
        allowed_pattern_names=frozenset(policy.allowed_pattern_names or ()),
        allow_generic_html_cards=policy.allow_generic_html_cards,
        allow_browser_required=policy.allow_browser_required,
        allow_ai_origin=policy.allow_ai_origin,
        allow_administrator_manual_origin=policy.allow_administrator_manual_origin,
        allow_imported_configuration=policy.allow_imported_configuration,
        allow_detail_page_enrichment=policy.allow_detail_page_enrichment,
        minimum_detector_confidence=policy.minimum_detector_confidence,
        minimum_events_found=policy.minimum_events_found,
        minimum_valid_events=policy.minimum_valid_events,
        minimum_valid_percentage=policy.minimum_valid_percentage,
        maximum_rejected_percentage=policy.maximum_rejected_percentage,
        minimum_canonical_url_coverage=policy.minimum_canonical_url_coverage,
        minimum_start_date_coverage=policy.minimum_start_date_coverage,
        minimum_start_date_parse_success=policy.minimum_start_date_parse_success,
        maximum_duplicate_rate=policy.maximum_duplicate_rate,
        maximum_warning_count=policy.maximum_warning_count,
        maximum_critical_warning_count=policy.maximum_critical_warning_count,
        minimum_distinct_events=policy.minimum_distinct_events,
        require_absolute_public_urls=policy.require_absolute_public_urls,
        require_zero_critical_warnings=policy.require_zero_critical_warnings,
        require_distinct_events=policy.require_distinct_events,
        generic_html_minimum_detector_confidence=(
            policy.generic_html_minimum_detector_confidence
        ),
        generic_html_minimum_events_found=policy.generic_html_minimum_events_found,
        generic_html_minimum_valid_events=policy.generic_html_minimum_valid_events,
        generic_html_minimum_valid_percentage=policy.generic_html_minimum_valid_percentage,
        generic_html_maximum_rejected_percentage=(
            policy.generic_html_maximum_rejected_percentage
        ),
        generic_html_minimum_required_field_confidence=(
            policy.generic_html_minimum_required_field_confidence
        ),
        generic_html_minimum_required_field_coverage=(
            policy.generic_html_minimum_required_field_coverage
        ),
        generic_html_minimum_date_format_confidence=(
            policy.generic_html_minimum_date_format_confidence
        ),
        generic_html_minimum_distinct_event_count=(
            policy.generic_html_minimum_distinct_event_count
        ),
        generic_html_reject_broad_canonical_selector=(
            policy.generic_html_reject_broad_canonical_selector
        ),
        generic_html_reject_unstable_required_selectors=(
            policy.generic_html_reject_unstable_required_selectors
        ),
        city_ids=frozenset(city_ids),
        role_ids=frozenset(role_ids),
    )


@dataclass(frozen=True)
class DecisionContext:
    """Everything the evaluation is allowed to see. Note what is absent: no
    Session, no Website row, no hostname, no page content."""

    policy: PolicySnapshot | None

    website_id: int
    website_is_archived: bool
    website_onboarding_status: str
    city_id: int | None
    city_is_active: bool

    detected_pattern: str | None = None
    detector_confidence: float = 0.0
    detection_status: str | None = None
    browser_required: bool = False
    blocked: bool = False
    registered_patterns: frozenset[str] = frozenset()

    configuration: SiteConfiguration | None = None
    configuration_origin: str | None = None
    configuration_version: int | None = None

    preview_run_id: int | None = None
    preview_status: str | None = None
    # The configuration version the preview actually ran against. Compared to
    # `configuration_version` to detect a draft saved after the preview.
    preview_configuration_version: int | None = None
    quality: PreviewQualityResult | None = None
    warnings: tuple[str, ...] = ()
    events_persisted_during_preview: int = 0

    missing_required_fields: tuple[str, ...] = ()
    field_candidates: tuple[dict[str, Any], ...] = ()
    date_format_candidates: tuple[dict[str, Any], ...] = ()
    detail_enrichment_used: bool = False

    submitted_by_user_id: int | None = None
    submitter_role_ids: frozenset[int] = frozenset()


@dataclass(frozen=True)
class AutoOnboardingDecisionResult:
    final_decision: str
    eligible_for_automatic_approval: bool
    eligible_for_automatic_activation: bool
    activation_policy_enabled: bool
    reasons_passed: tuple[str, ...]
    reasons_failed: tuple[str, ...]
    metrics_snapshot: dict[str, Any] = field(default_factory=dict)
    thresholds_snapshot: dict[str, Any] = field(default_factory=dict)
    policy_id: int | None = None
    policy_version: int | None = None
    policy_name: str | None = None
    detected_pattern: str | None = None
    detector_confidence: float | None = None
    configuration_origin: str | None = None
    configuration_version: int | None = None
    preview_run_id: int | None = None
    preview_status: str | None = None
    evaluated_roles: tuple[int, ...] = ()


# --- Rule bookkeeping -------------------------------------------------------


class _Rules:
    """Collects pass/fail reasons and the metrics/thresholds actually
    consulted, so the snapshot stays compact and reproducible rather than a
    copy of every policy column."""

    def __init__(self) -> None:
        self.passed: list[str] = []
        self.failed: list[str] = []
        self.metrics: dict[str, Any] = {}
        self.thresholds: dict[str, Any] = {}

    def check(self, ok: bool, passed_reason: str, failed_reason: str) -> bool:
        (self.passed if ok else self.failed).append(passed_reason if ok else failed_reason)
        return ok

    def at_least(self, name: str, value, threshold, *, label: str) -> bool:
        self.metrics[name] = value
        self.thresholds[f"minimum_{name}"] = threshold
        return self.check(
            value is not None and value >= threshold,
            f"{label} {_fmt(value)} meets minimum {_fmt(threshold)}",
            f"{label} {_fmt(value)} is below the minimum {_fmt(threshold)}",
        )

    def at_most(self, name: str, value, threshold, *, label: str) -> bool:
        self.metrics[name] = value
        self.thresholds[f"maximum_{name}"] = threshold
        return self.check(
            value is not None and value <= threshold,
            f"{label} {_fmt(value)} is within the maximum {_fmt(threshold)}",
            f"{label} {_fmt(value)} exceeds the maximum {_fmt(threshold)}",
        )


def _fmt(value) -> str:
    if isinstance(value, float):
        return f"{value:.0%}" if 0.0 <= value <= 1.0 else f"{value:.2f}"
    return str(value)


def _selector_is_unstable(selector: str) -> str | None:
    """Returns why a selector is unstable, or None if it is fine."""
    if _POSITIONAL_SELECTOR_RE.search(selector):
        return "uses a positional selector"
    if selector_parts(selector) > 3:
        return "is an excessively deep chain"
    for token in re.findall(r"\.([A-Za-z0-9_-]+)", selector):
        if not is_stable_class_token(token):
            return f"relies on the generated or numeric class '{token}'"
    return None


# --- The service ------------------------------------------------------------


class AutoOnboardingDecisionService:
    def evaluate(self, context: DecisionContext) -> AutoOnboardingDecisionResult:
        rules = _Rules()
        policy = context.policy

        common = {
            "detected_pattern": context.detected_pattern,
            "detector_confidence": context.detector_confidence,
            "configuration_origin": context.configuration_origin,
            "configuration_version": context.configuration_version,
            "preview_run_id": context.preview_run_id,
            "preview_status": context.preview_status,
            "evaluated_roles": tuple(sorted(context.submitter_role_ids)),
        }

        if policy is None:
            return AutoOnboardingDecisionResult(
                final_decision=NO_APPLICABLE_POLICY,
                eligible_for_automatic_approval=False,
                eligible_for_automatic_activation=False,
                activation_policy_enabled=False,
                reasons_passed=(),
                reasons_failed=("no automatic-onboarding policy applies to this source",),
                **common,
            )

        policy_fields = {
            "policy_id": policy.policy_id,
            "policy_version": policy.version,
            "policy_name": policy.name,
        }

        # A policy that is switched off, or whose automatic configuration or
        # preview steps are disabled, cannot have produced a result worth
        # acting on.
        if not (
            policy.active
            and policy.automatic_configuration_enabled
            and policy.automatic_preview_enabled
        ):
            return AutoOnboardingDecisionResult(
                final_decision=POLICY_DISABLED,
                eligible_for_automatic_approval=False,
                eligible_for_automatic_activation=False,
                activation_policy_enabled=policy.automatic_activation_enabled,
                reasons_passed=(),
                reasons_failed=(
                    f"policy '{policy.name}' does not have automatic configuration, preview "
                    "and activity all enabled",
                ),
                **policy_fields,
                **common,
            )

        self._check_policy_scope(rules, context, policy)
        self._check_pattern(rules, context, policy)
        self._check_detection(rules, context, policy)
        self._check_website_and_city(rules, context, policy)
        self._check_configuration(rules, context, policy)
        self._check_preview(rules, context, policy)
        if context.detected_pattern == GENERIC_HTML_PATTERN:
            self._check_generic_html(rules, context, policy)

        approval_enabled = rules.check(
            policy.automatic_approval_enabled,
            "automatic approval is enabled by policy",
            "automatic approval is disabled by policy",
        )
        eligible = approval_enabled and not rules.failed

        # Conditional only: the policy permits activation and nothing in this
        # evaluation forbids it. Final activation eligibility is re-checked
        # after approval actually succeeds.
        activation_eligible = eligible and policy.automatic_activation_enabled
        if eligible and not policy.automatic_activation_enabled:
            rules.passed.append("automatic activation is disabled by policy; approval only")

        return AutoOnboardingDecisionResult(
            final_decision=(
                AUTOMATIC_APPROVAL_ALLOWED if eligible else AUTOMATIC_APPROVAL_DENIED
            ),
            eligible_for_automatic_approval=eligible,
            eligible_for_automatic_activation=activation_eligible,
            activation_policy_enabled=policy.automatic_activation_enabled,
            reasons_passed=tuple(rules.passed),
            reasons_failed=tuple(rules.failed),
            metrics_snapshot=rules.metrics,
            thresholds_snapshot=rules.thresholds,
            **policy_fields,
            **common,
        )

    # --- rule groups --------------------------------------------------------

    def _check_policy_scope(self, rules, context: DecisionContext, policy: PolicySnapshot):
        if policy.city_ids:
            rules.check(
                context.city_id in policy.city_ids,
                "the source's city is within the policy's scope",
                "the source's city is outside the policy's scope",
            )
        if policy.role_ids:
            rules.check(
                bool(context.submitter_role_ids & policy.role_ids),
                "the submitter holds a role the policy permits",
                "the submitter does not hold a role the policy permits",
            )

    def _check_pattern(self, rules, context: DecisionContext, policy: PolicySnapshot):
        pattern = context.detected_pattern
        rules.check(
            bool(pattern) and pattern in context.registered_patterns,
            f"pattern '{pattern}' is registered",
            f"pattern '{pattern}' is not a registered extraction pattern",
        )
        rules.check(
            bool(pattern) and pattern in policy.allowed_pattern_names,
            f"pattern '{pattern}' is allowed by policy",
            f"pattern '{pattern}' is not in the policy's allowed patterns",
        )
        if pattern == GENERIC_HTML_PATTERN:
            rules.check(
                policy.allow_generic_html_cards,
                "generic HTML card extraction is explicitly allowed by policy",
                "generic HTML card extraction is not enabled for automatic approval",
            )
        if context.browser_required:
            rules.check(
                policy.allow_browser_required,
                "browser rendering is explicitly allowed by policy",
                "this source requires browser rendering, which the policy denies",
            )

    def _check_detection(self, rules, context: DecisionContext, policy: PolicySnapshot):
        threshold = (
            policy.generic_html_minimum_detector_confidence
            if context.detected_pattern == GENERIC_HTML_PATTERN
            else policy.minimum_detector_confidence
        )
        rules.at_least(
            "detector_confidence",
            context.detector_confidence,
            threshold,
            label="detector confidence",
        )
        rules.check(
            not context.blocked,
            "the source was not blocked",
            "the source was blocked, challenged, or required credentials",
        )
        rules.check(
            context.detection_status != "unsupported",
            "detection produced a supported pattern",
            "detection reported the source as unsupported",
        )

    def _check_website_and_city(self, rules, context: DecisionContext, policy: PolicySnapshot):
        rules.check(
            not context.website_is_archived,
            "the matched website is not archived",
            "the matched website is archived and can never qualify automatically",
        )
        rules.check(
            context.city_id is not None,
            "the website is assigned to a city",
            "the website has no city, so it could not be approved at all",
        )
        rules.check(
            context.city_is_active,
            "the website's city is active",
            "the website's city is inactive",
        )

    def _check_configuration(self, rules, context: DecisionContext, policy: PolicySnapshot):
        config = context.configuration
        if config is None:
            rules.failed.append("there is no draft configuration to approve")
            return

        rules.check(
            config.pattern_name == context.detected_pattern,
            "the proposed configuration matches the detected pattern",
            (
                f"the configuration is for '{config.pattern_name}' but "
                f"'{context.detected_pattern}' was detected"
            ),
        )

        origin = context.configuration_origin
        rules.check(
            self._origin_allowed(origin, policy),
            f"configuration origin '{origin}' is allowed by policy",
            (
                f"configuration origin '{origin}' is not allowed for automatic approval"
                if origin
                else "the configuration's origin is unknown, so it cannot be auto-approved"
            ),
        )

        rules.check(
            not context.missing_required_fields,
            "every required field is present in the configuration",
            "required field(s) missing: " + ", ".join(context.missing_required_fields),
        )

        if policy.require_absolute_public_urls:
            rules.check(
                self._urls_are_safe(config),
                "every configured URL is absolute, public and passes SSRF validation",
                "a configured URL is not a safe, absolute public URL",
            )

        # `SiteConfiguration` already forbids credential headers at the schema
        # level; this re-checks rather than assuming, because approval is the
        # last gate before a configuration becomes live.
        rules.check(
            not config.fetch.headers,
            "no custom request headers are configured",
            "custom request headers are configured and were not reviewed by a human",
        )

        if context.detail_enrichment_used:
            rules.check(
                policy.allow_detail_page_enrichment,
                "detail-page enrichment is allowed by policy",
                "this configuration uses detail-page enrichment, which the policy denies",
            )

    def _origin_allowed(self, origin: str | None, policy: PolicySnapshot) -> bool:
        if origin in DETERMINISTIC_ORIGINS:
            return True
        if origin == ORIGIN_ADMINISTRATOR_MANUAL:
            return policy.allow_administrator_manual_origin
        if origin == ORIGIN_AI_SUGGESTED:
            return policy.allow_ai_origin
        if origin == ORIGIN_IMPORTED:
            return policy.allow_imported_configuration
        return False  # unknown origin fails closed

    def _urls_are_safe(self, config: SiteConfiguration) -> bool:
        for url in (config.listing_url, config.api_endpoint):
            if not url:
                continue
            try:
                validate_public_url(url)
            except UnsafeURLError:
                return False
        return bool(config.listing_url or config.api_endpoint)

    def _check_preview(self, rules, context: DecisionContext, policy: PolicySnapshot):
        quality = context.quality
        if quality is None or context.preview_run_id is None:
            rules.failed.append("no preview run is available for the current configuration")
            return

        rules.check(
            context.preview_status in _ACCEPTABLE_PREVIEW_STATUSES,
            f"preview status '{context.preview_status}' is acceptable",
            f"preview status '{context.preview_status}' is not acceptable",
        )
        # The same staleness rule manual approval enforces, checked here too so
        # a decision can never be based on a preview of an older draft.
        rules.check(
            context.preview_configuration_version == context.configuration_version,
            (
                f"the preview ran against the current draft version "
                f"{context.configuration_version}"
            ),
            (
                f"the preview ran against configuration version "
                f"{context.preview_configuration_version} but the current draft is "
                f"{context.configuration_version}"
            ),
        )
        rules.check(
            context.events_persisted_during_preview == 0,
            "the preview persisted no events",
            "the preview appears to have persisted events, which must never happen",
        )

        generic = context.detected_pattern == GENERIC_HTML_PATTERN
        rules.at_least(
            "events_found",
            quality.candidates_found,
            policy.generic_html_minimum_events_found if generic else policy.minimum_events_found,
            label="events found",
        )
        rules.at_least(
            "valid_events",
            quality.valid_count,
            policy.generic_html_minimum_valid_events if generic else policy.minimum_valid_events,
            label="valid events",
        )
        rules.at_least(
            "valid_percentage",
            quality.valid_percentage,
            policy.generic_html_minimum_valid_percentage
            if generic
            else policy.minimum_valid_percentage,
            label="valid percentage",
        )
        rules.at_most(
            "rejected_percentage",
            quality.rejected_percentage,
            policy.generic_html_maximum_rejected_percentage
            if generic
            else policy.maximum_rejected_percentage,
            label="rejected percentage",
        )
        rules.check(
            quality.valid_count > 0,
            "the preview produced at least one valid event",
            "the preview produced no valid events",
        )

        coverage = quality.required_field_coverage or {}
        rules.at_least(
            "canonical_url_coverage",
            coverage.get("canonical_url", 0.0),
            policy.minimum_canonical_url_coverage,
            label="canonical URL coverage",
        )
        rules.at_least(
            "start_date_coverage",
            coverage.get("start_date", 0.0),
            policy.minimum_start_date_coverage,
            label="start-date coverage",
        )
        rules.at_least(
            "start_date_parse_success",
            quality.date_parse_success_rate,
            policy.minimum_start_date_parse_success,
            label="start-date parse success",
        )
        rules.at_least(
            "url_validity",
            quality.url_validity_rate,
            policy.minimum_canonical_url_coverage,
            label="canonical URL validity",
        )
        rules.at_most(
            "duplicate_rate", quality.duplicate_rate, policy.maximum_duplicate_rate,
            label="duplicate rate",
        )
        rules.at_most(
            "warning_count", quality.warning_count, policy.maximum_warning_count,
            label="warning count",
        )

        critical = sum(
            1
            for warning in context.warnings
            if any(marker in warning.lower() for marker in CRITICAL_WARNING_MARKERS)
        )
        limit = (
            0
            if policy.require_zero_critical_warnings
            else policy.maximum_critical_warning_count
        )
        rules.at_most("critical_warning_count", critical, limit, label="critical warning count")

        if policy.require_distinct_events:
            # Derived from the duplicate rate the preview already computed:
            # duplicate_rate is duplicates/valid, so this is the count of
            # valid events that were not collapsed as duplicates.
            distinct = round(quality.valid_count * (1.0 - quality.duplicate_rate))
            rules.at_least(
                "distinct_events",
                distinct,
                policy.generic_html_minimum_distinct_event_count
                if generic
                else policy.minimum_distinct_events,
                label="distinct events",
            )

    def _check_generic_html(self, rules, context: DecisionContext, policy: PolicySnapshot):
        """Extra scrutiny for a configuration inferred from markup: there is
        no schema behind it, so the *selectors themselves* are evidence."""
        config = context.configuration
        if config is None:
            return

        accepted = {
            candidate.get("field"): candidate
            for candidate in context.field_candidates
            if candidate.get("accepted")
        }
        for field_name in REQUIRED_SELECTOR_FIELDS:
            candidate = accepted.get(field_name)
            if candidate is None:
                # A required field resolved from the detail page counts, but
                # anything genuinely absent is already reported by the
                # missing-required-fields rule; don't double-report.
                if f"detail_{field_name}" not in config.field_selectors:
                    continue
                candidate = {}
            rules.at_least(
                f"{field_name}_confidence",
                candidate.get("confidence", 0.0),
                policy.generic_html_minimum_required_field_confidence,
                label=f"'{field_name}' proposal confidence",
            )
            rules.at_least(
                f"{field_name}_coverage",
                candidate.get("coverage", 0.0),
                policy.generic_html_minimum_required_field_coverage,
                label=f"'{field_name}' coverage",
            )

        date_formats = [c for c in context.date_format_candidates if c.get("accepted")]
        best_date_rate = max((c.get("match_rate", 0.0) for c in date_formats), default=0.0)
        rules.at_least(
            "date_format_confidence",
            best_date_rate,
            policy.generic_html_minimum_date_format_confidence,
            label="date-format confidence",
        )

        canonical = config.field_selectors.get("canonical_url")
        if canonical is not None and policy.generic_html_reject_broad_canonical_selector:
            rules.check(
                canonical.selector.strip() not in _BROAD_ANCHOR_SELECTORS,
                "the canonical URL selector is specific to the event link",
                f"the canonical URL selector '{canonical.selector}' is too broad",
            )

        if policy.generic_html_reject_unstable_required_selectors:
            for field_name in REQUIRED_SELECTOR_FIELDS:
                selector_config = config.field_selectors.get(field_name)
                if selector_config is None:
                    continue
                problem = _selector_is_unstable(selector_config.selector)
                rules.check(
                    problem is None,
                    f"the '{field_name}' selector is stable",
                    f"the '{field_name}' selector {problem}",
                )
