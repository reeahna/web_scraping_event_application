"""Increment 2: qualification rules, evaluated purely.

Every test builds a DecisionContext directly, so these exercise the rules
themselves rather than the pipeline that will later feed them. Nothing here
approves or activates anything.

Policies are persisted rather than constructed in memory: SQLAlchemy applies
column defaults at flush time, so a transient instance would carry None for
every threshold. Persisting keeps the model the single source of those
defaults instead of duplicating them in this file.
"""

from __future__ import annotations

import dataclasses
import itertools

import pytest

from app.core.auto_onboarding import (
    ACTOR_SYSTEM,
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
from app.extraction.inference.types import PreviewQualityResult
from app.extraction.registry import REGISTRY
from app.models.audit_log import AuditLog
from app.models.auto_onboarding_policy import AutoOnboardingPolicy
from app.repositories.auto_onboarding import list_decisions_for_website
from app.schemas.extraction import SiteConfiguration
from app.services.auto_onboarding_decision import (
    AutoOnboardingDecisionService,
    DecisionContext,
    PolicySnapshot,
    snapshot_policy,
)
from app.services.auto_onboarding_persistence import record_decision

SERVICE = AutoOnboardingDecisionService()
LISTING_URL = "https://venue.example.org/events"
_names = itertools.count(1)


@pytest.fixture
def make_policy(db_session):
    def _make(**overrides) -> AutoOnboardingPolicy:
        policy = AutoOnboardingPolicy(
            name=f"Test policy {next(_names)}",
            active=True,
            version=3,
            automatic_approval_enabled=True,
            automatic_activation_enabled=False,
            allowed_pattern_names=[
                "json_ld_event",
                "the_events_calendar",
                "generic_html_cards",
            ],
        )
        db_session.add(policy)
        db_session.commit()
        db_session.refresh(policy)
        for key, value in overrides.items():
            setattr(policy, key, value)
        db_session.commit()
        db_session.refresh(policy)
        return policy

    return _make


@pytest.fixture
def base_policy(make_policy):
    """Permissive enough that a good structured source qualifies, so each
    test can break exactly one thing."""
    return make_policy()


@pytest.fixture
def generic_policy(make_policy):
    """Adds the explicit generic_html_cards permission, so the generic tests
    exercise the stricter thresholds rather than the enablement flag."""
    return make_policy(allow_generic_html_cards=True)


# --- context builders --------------------------------------------------------


def _quality(**overrides) -> PreviewQualityResult:
    defaults = {
        "candidates_found": 8,
        "valid_count": 8,
        "rejected_count": 0,
        "valid_percentage": 1.0,
        "rejected_percentage": 0.0,
        "required_field_coverage": {"title": 1.0, "start_date": 1.0, "canonical_url": 1.0},
        "date_parse_success_rate": 1.0,
        "url_validity_rate": 1.0,
        "duplicate_rate": 0.0,
        "warning_count": 0,
        "pagination_truncated": False,
        "detail_fetch_used": False,
        "pages_fetched": 1,
    }
    defaults.update(overrides)
    return PreviewQualityResult(**defaults)


def _config(**overrides) -> SiteConfiguration:
    values = {"pattern_name": "json_ld_event", "listing_url": LISTING_URL}
    values.update(overrides)
    return SiteConfiguration(**values)


def _snapshot(policy):
    if policy is None or isinstance(policy, PolicySnapshot):
        return policy
    return snapshot_policy(policy)


def _context(policy, **overrides) -> DecisionContext:
    base = {
        "policy": _snapshot(policy),
        "website_id": 1,
        "website_is_archived": False,
        "website_onboarding_status": "detected",
        "city_id": 7,
        "city_is_active": True,
        "detected_pattern": "json_ld_event",
        "detector_confidence": 0.95,
        "detection_status": "success",
        "browser_required": False,
        "blocked": False,
        "registered_patterns": frozenset(REGISTRY.names()),
        "configuration": _config(),
        "configuration_origin": ORIGIN_DETERMINISTIC_STRUCTURED,
        "configuration_version": 4,
        "preview_run_id": 12,
        "preview_status": "success",
        "preview_configuration_version": 4,
        "quality": _quality(),
        "warnings": (),
        "missing_required_fields": (),
        "detail_enrichment_used": False,
    }
    base.update(overrides)
    return DecisionContext(**base)


def _evaluate(policy, **overrides):
    return SERVICE.evaluate(_context(policy, **overrides))


def _denied_for(result, fragment: str) -> bool:
    return any(fragment in reason for reason in result.reasons_failed)


# --- baseline ---------------------------------------------------------------


def test_a_fully_qualifying_structured_source_is_allowed(base_policy):
    result = _evaluate(base_policy)
    assert result.final_decision == AUTOMATIC_APPROVAL_ALLOWED
    assert result.eligible_for_automatic_approval is True
    assert result.reasons_failed == ()
    assert result.reasons_passed
    # Evaluation never claims the action happened.
    assert result.final_decision != "automatically_approved"


def test_the_result_carries_a_compact_reproducible_snapshot(base_policy):
    result = _evaluate(base_policy)
    assert result.metrics_snapshot["valid_percentage"] == 1.0
    assert result.thresholds_snapshot["minimum_valid_percentage"] == 0.9
    assert result.policy_version == 3
    # Only thresholds actually consulted are recorded, not every policy column.
    assert "generic_html_minimum_valid_events" not in result.thresholds_snapshot
    assert len(result.thresholds_snapshot) < 20


# --- policy-level gates ------------------------------------------------------


def test_no_applicable_policy_means_manual_review():
    result = SERVICE.evaluate(_context(None))
    assert result.final_decision == NO_APPLICABLE_POLICY
    assert result.eligible_for_automatic_approval is False


# --- Phase 8G gates ----------------------------------------------------------


def test_date_range_success_gate_denies_a_low_rate(make_policy):
    policy = make_policy(
        require_date_range_parse_success=True, minimum_date_range_parse_success=0.95
    )
    result = _evaluate(policy, quality=_quality(range_parse_success_rate=0.5))
    assert result.final_decision == AUTOMATIC_APPROVAL_DENIED
    assert _denied_for(result, "date-range parse success")


def test_date_range_success_gate_is_off_by_default(base_policy):
    # A low range rate does not block when the policy hasn't opted in.
    result = _evaluate(base_policy, quality=_quality(range_parse_success_rate=0.0))
    assert result.final_decision == AUTOMATIC_APPROVAL_ALLOWED


def test_require_geographic_filter_denies_when_none_configured(make_policy):
    policy = make_policy(require_geographic_filter=True)
    result = _evaluate(policy)  # default config has no geographic_filters
    assert result.final_decision == AUTOMATIC_APPROVAL_DENIED
    assert _denied_for(result, "geographic filter")


def test_require_geographic_filter_passes_when_configured(make_policy):
    from app.schemas.geographic import GeographicFilterConfig

    policy = make_policy(require_geographic_filter=True)
    result = _evaluate(
        policy,
        configuration=_config(geographic_filters=GeographicFilterConfig(localities=["Springfield"])),
    )
    assert result.final_decision == AUTOMATIC_APPROVAL_ALLOWED


def test_geographic_inclusion_rate_gate(make_policy):
    policy = make_policy(minimum_geographic_inclusion_rate=0.9)
    result = _evaluate(policy, quality=_quality(geographic_inclusion_rate=0.4))
    assert result.final_decision == AUTOMATIC_APPROVAL_DENIED
    assert _denied_for(result, "geographic inclusion rate")


@pytest.mark.parametrize(
    "disabled", ["active", "automatic_configuration_enabled", "automatic_preview_enabled"]
)
def test_a_disabled_policy_short_circuits(make_policy, disabled):
    result = _evaluate(make_policy(**{disabled: False}))
    assert result.final_decision == POLICY_DISABLED
    assert result.eligible_for_automatic_approval is False


def test_approval_disabled_denies_but_still_reports_the_quality_rules(make_policy):
    result = _evaluate(make_policy(automatic_approval_enabled=False))
    assert result.final_decision == AUTOMATIC_APPROVAL_DENIED
    assert _denied_for(result, "automatic approval is disabled")
    # The rest of the evaluation still ran, so an administrator can see the
    # source *would* have qualified.
    assert any("valid percentage" in reason for reason in result.reasons_passed)


def test_activation_eligibility_needs_approval_eligibility_and_the_policy_flag(
    base_policy, make_policy
):
    assert _evaluate(base_policy).eligible_for_automatic_activation is False
    enabled = make_policy(automatic_activation_enabled=True)
    assert _evaluate(enabled).eligible_for_automatic_activation is True
    # A source that cannot be approved can never be activation-eligible.
    assert _evaluate(enabled, city_is_active=False).eligible_for_automatic_activation is False


# --- scope -------------------------------------------------------------------


def test_a_city_outside_the_policy_scope_fails(base_policy):
    scoped = snapshot_policy(base_policy, city_ids=[99])
    assert _denied_for(_evaluate(scoped), "outside the policy's scope")


def test_a_submitter_without_a_permitted_role_fails(base_policy):
    scoped = snapshot_policy(base_policy, role_ids=[5])
    assert _denied_for(_evaluate(scoped, submitter_role_ids=frozenset({9})), "role")
    ok = _evaluate(scoped, submitter_role_ids=frozenset({5, 9}))
    assert ok.eligible_for_automatic_approval is True
    assert ok.evaluated_roles == (5, 9)


# --- pattern -----------------------------------------------------------------


def test_a_pattern_outside_the_allowed_list_fails(make_policy):
    result = _evaluate(make_policy(allowed_pattern_names=["wordpress_rest"]))
    assert _denied_for(result, "not in the policy's allowed patterns")


def test_an_unregistered_pattern_fails(base_policy):
    result = _evaluate(base_policy, detected_pattern="not_a_real_pattern")
    assert _denied_for(result, "not a registered extraction pattern")


def test_a_browser_required_source_fails_unless_explicitly_permitted(base_policy, make_policy):
    assert _denied_for(
        _evaluate(base_policy, browser_required=True), "requires browser rendering"
    )
    permitted = make_policy(allow_browser_required=True)
    assert _evaluate(permitted, browser_required=True).eligible_for_automatic_approval


def test_a_blocked_or_unsupported_source_fails(base_policy):
    assert _denied_for(_evaluate(base_policy, blocked=True), "blocked")
    assert _denied_for(_evaluate(base_policy, detection_status="unsupported"), "unsupported")


def test_detector_confidence_below_the_threshold_fails(base_policy):
    assert _denied_for(_evaluate(base_policy, detector_confidence=0.5), "detector confidence")


# --- website and city --------------------------------------------------------


def test_an_archived_website_can_never_qualify(base_policy):
    result = _evaluate(base_policy, website_is_archived=True)
    assert _denied_for(result, "archived")
    assert result.eligible_for_automatic_approval is False


def test_an_inactive_or_missing_city_fails(base_policy):
    assert _denied_for(_evaluate(base_policy, city_is_active=False), "city is inactive")
    assert _denied_for(_evaluate(base_policy, city_id=None, city_is_active=False), "no city")


# --- configuration -----------------------------------------------------------


def test_a_missing_configuration_fails(base_policy):
    assert _denied_for(_evaluate(base_policy, configuration=None), "no draft configuration")


def test_a_configuration_for_a_different_pattern_fails(base_policy):
    result = _evaluate(base_policy, configuration=_config(pattern_name="wordpress_rest"))
    assert _denied_for(result, "but 'json_ld_event' was detected")


def test_a_missing_required_field_fails(base_policy):
    result = _evaluate(base_policy, missing_required_fields=("start_date",))
    assert _denied_for(result, "required field(s) missing: start_date")


@pytest.mark.parametrize(
    ("origin", "allowed"),
    [
        (ORIGIN_DETERMINISTIC_STRUCTURED, True),
        (ORIGIN_DETERMINISTIC_GENERIC_HTML, True),
        (ORIGIN_ADMINISTRATOR_MANUAL, False),
        (ORIGIN_AI_SUGGESTED, False),
        (ORIGIN_IMPORTED, False),
        (None, False),
        ("something_new", False),
    ],
)
def test_configuration_origin_rules(base_policy, origin, allowed):
    result = _evaluate(base_policy, configuration_origin=origin)
    assert result.eligible_for_automatic_approval is allowed, result.reasons_failed


def test_an_ai_origin_configuration_only_qualifies_when_explicitly_permitted(
    base_policy, make_policy
):
    denied = _evaluate(base_policy, configuration_origin=ORIGIN_AI_SUGGESTED)
    assert denied.eligible_for_automatic_approval is False

    permitted = make_policy(allow_ai_origin=True)
    allowed = _evaluate(permitted, configuration_origin=ORIGIN_AI_SUGGESTED)
    # Even when permitted it still had to pass every deterministic check.
    assert allowed.eligible_for_automatic_approval is True
    assert any("valid percentage" in reason for reason in allowed.reasons_passed)


def test_configured_request_headers_block_automatic_approval(base_policy):
    config = _config(fetch={"headers": {"X-Api-Key": "public-token"}})
    assert _denied_for(_evaluate(base_policy, configuration=config), "custom request headers")


def test_detail_page_enrichment_denied_by_policy_fails(make_policy):
    policy = make_policy(allow_detail_page_enrichment=False)
    assert _denied_for(_evaluate(policy, detail_enrichment_used=True), "detail-page")


# --- preview -----------------------------------------------------------------


def test_a_missing_preview_fails(base_policy):
    assert _denied_for(
        _evaluate(base_policy, quality=None, preview_run_id=None), "no preview run"
    )


def test_a_stale_preview_fails(base_policy):
    result = _evaluate(base_policy, preview_configuration_version=3, configuration_version=4)
    assert _denied_for(result, "configuration version 3 but the current draft is 4")


def test_an_unacceptable_preview_status_fails(base_policy):
    assert _denied_for(_evaluate(base_policy, preview_status="failed"), "not acceptable")


def test_zero_valid_events_can_never_qualify(base_policy):
    quality = _quality(
        valid_count=0, valid_percentage=0.0, rejected_count=8, rejected_percentage=1.0
    )
    result = _evaluate(base_policy, quality=quality)
    assert result.eligible_for_automatic_approval is False
    assert _denied_for(result, "no valid events")


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"candidates_found": 2, "valid_count": 2}, "events found"),
        ({"valid_count": 2}, "valid events"),
        ({"valid_percentage": 0.5}, "valid percentage"),
        ({"rejected_percentage": 0.5}, "rejected percentage"),
        ({"date_parse_success_rate": 0.5}, "start-date parse success"),
        ({"url_validity_rate": 0.5}, "canonical URL validity"),
        ({"duplicate_rate": 0.9}, "duplicate rate"),
        ({"warning_count": 99}, "warning count"),
        (
            {"required_field_coverage": {"canonical_url": 0.5, "start_date": 1.0}},
            "canonical URL coverage",
        ),
        (
            {"required_field_coverage": {"canonical_url": 1.0, "start_date": 0.5}},
            "start-date coverage",
        ),
    ],
)
def test_quality_thresholds_each_deny_independently(base_policy, overrides, fragment):
    result = _evaluate(base_policy, quality=_quality(**overrides))
    assert _denied_for(result, fragment), result.reasons_failed


def test_a_critical_warning_denies_approval(base_policy):
    result = _evaluate(base_policy, warnings=("detail_page_fetch_failed:https://x.example.org",))
    assert _denied_for(result, "critical warning")


def test_ordinary_warnings_do_not_count_as_critical(base_policy):
    result = _evaluate(base_policy, warnings=("max_events_reached",))
    assert result.eligible_for_automatic_approval is True


def test_too_few_distinct_events_fails(base_policy):
    # 8 valid but 87.5% duplicates leaves 1 distinct event.
    result = _evaluate(base_policy, quality=_quality(duplicate_rate=0.875))
    assert _denied_for(result, "distinct events")


def test_events_persisted_during_preview_is_a_hard_failure(base_policy):
    result = _evaluate(base_policy, events_persisted_during_preview=3)
    assert _denied_for(result, "persisted events")


# --- generic_html_cards ------------------------------------------------------


GENERIC_FIELDS = (
    {"field": "title", "accepted": True, "confidence": 0.92, "coverage": 1.0},
    {"field": "canonical_url", "accepted": True, "confidence": 0.89, "coverage": 1.0},
    {"field": "start_datetime", "accepted": True, "confidence": 0.85, "coverage": 1.0},
)
GENERIC_DATE_FORMATS = ({"format": "%B %d, %Y", "accepted": True, "match_rate": 1.0},)


def _generic_config(**selectors) -> SiteConfiguration:
    base = {
        "title": {"kind": "css", "selector": "h3.event-title"},
        "canonical_url": {"kind": "css", "selector": "h3.event-title a", "attribute": "href"},
        "start_datetime": {"kind": "css", "selector": "span.event-date"},
    }
    base.update(selectors)
    return SiteConfiguration(
        pattern_name="generic_html_cards",
        listing_url=LISTING_URL,
        event_container_selector="div.event-card",
        field_selectors=base,
    )


def _generic_context(policy, **overrides) -> DecisionContext:
    defaults = {
        "detected_pattern": "generic_html_cards",
        "detector_confidence": 0.9,
        "configuration": _generic_config(),
        "configuration_origin": ORIGIN_DETERMINISTIC_GENERIC_HTML,
        "field_candidates": GENERIC_FIELDS,
        "date_format_candidates": GENERIC_DATE_FORMATS,
        "quality": _quality(),
    }
    defaults.update(overrides)
    return _context(policy, **defaults)


def test_generic_html_is_denied_by_default_even_when_everything_else_passes(base_policy):
    result = SERVICE.evaluate(_generic_context(base_policy))
    assert result.eligible_for_automatic_approval is False
    assert _denied_for(result, "generic HTML card extraction is not enabled")


def test_generic_html_qualifies_when_explicitly_enabled_and_high_quality(generic_policy):
    result = SERVICE.evaluate(_generic_context(generic_policy))
    assert result.eligible_for_automatic_approval is True, result.reasons_failed


def test_generic_html_uses_the_stricter_detector_threshold(generic_policy):
    # 0.82 clears the general 0.80 bar but not the generic 0.85 one.
    result = SERVICE.evaluate(_generic_context(generic_policy, detector_confidence=0.82))
    assert _denied_for(result, "detector confidence")


def test_generic_html_uses_stricter_count_and_percentage_thresholds(generic_policy):
    too_few = SERVICE.evaluate(
        _generic_context(generic_policy, quality=_quality(candidates_found=4, valid_count=4))
    )
    assert _denied_for(too_few, "events found")

    too_low = SERVICE.evaluate(
        _generic_context(
            generic_policy,
            quality=_quality(valid_count=7, valid_percentage=0.92, rejected_percentage=0.08),
        )
    )
    assert _denied_for(too_low, "valid percentage")


@pytest.mark.parametrize("field_name", ["title", "canonical_url", "start_datetime"])
def test_a_low_confidence_required_field_proposal_fails(generic_policy, field_name):
    candidates = tuple(
        {**c, "confidence": 0.4} if c["field"] == field_name else c for c in GENERIC_FIELDS
    )
    result = SERVICE.evaluate(_generic_context(generic_policy, field_candidates=candidates))
    assert _denied_for(result, f"'{field_name}' proposal confidence")


def test_insufficient_required_field_coverage_fails(generic_policy):
    candidates = tuple(
        {**c, "coverage": 0.6} if c["field"] == "start_datetime" else c for c in GENERIC_FIELDS
    )
    result = SERVICE.evaluate(_generic_context(generic_policy, field_candidates=candidates))
    assert _denied_for(result, "'start_datetime' coverage")


def test_a_low_confidence_date_format_fails(generic_policy):
    formats = ({"format": "%B %d, %Y", "accepted": True, "match_rate": 0.62},)
    result = SERVICE.evaluate(_generic_context(generic_policy, date_format_candidates=formats))
    assert _denied_for(result, "date-format confidence")


def test_a_broad_canonical_url_selector_fails(generic_policy):
    config = _generic_config(canonical_url={"kind": "css", "selector": "a", "attribute": "href"})
    result = SERVICE.evaluate(_generic_context(generic_policy, configuration=config))
    assert _denied_for(result, "too broad")


@pytest.mark.parametrize(
    ("selector", "fragment"),
    [
        ("div.card:nth-child(2) h3", "positional selector"),
        ("div.a div.b div.c span", "deep chain"),
        ("h3.x7f3a91b2", "generated or numeric class"),
    ],
)
def test_an_unstable_required_selector_fails(generic_policy, selector, fragment):
    config = _generic_config(title={"kind": "css", "selector": selector})
    result = SERVICE.evaluate(_generic_context(generic_policy, configuration=config))
    assert _denied_for(result, fragment), result.reasons_failed


def test_generic_html_requires_more_distinct_events_than_structured(generic_policy):
    # 5 valid with 20% duplicates leaves 4 distinct: enough for the general
    # bar of 3, short of the generic bar of 5.
    quality = _quality(candidates_found=5, valid_count=5, duplicate_rate=0.2)
    result = SERVICE.evaluate(_generic_context(generic_policy, quality=quality))
    assert _denied_for(result, "distinct events")


def test_only_a_changed_threshold_makes_a_failing_source_pass(generic_policy, make_policy):
    """Sanity check on the shape of the rules: the sole route from denied to
    allowed is a different policy value, never an identity-based branch."""
    strict = _generic_context(generic_policy, detector_confidence=0.82)
    assert SERVICE.evaluate(strict).eligible_for_automatic_approval is False

    relaxed = make_policy(
        allow_generic_html_cards=True, generic_html_minimum_detector_confidence=0.80
    )
    relaxed_context = dataclasses.replace(strict, policy=snapshot_policy(relaxed))
    assert SERVICE.evaluate(relaxed_context).eligible_for_automatic_approval is True


# --- append-only persistence -------------------------------------------------


@pytest.fixture
def website(db_session, make_city, make_website):
    city = make_city(name="Persist City", slug="persist-city")
    return make_website(city, name="Persist Site", base_url="https://persist.example.org")


@pytest.fixture
def preview_run(db_session, website):
    """A real ExtractionRun, because the decision row has a foreign key to
    one — a decision must always point at a preview that actually exists."""
    from datetime import UTC, datetime

    from app.repositories.extraction_run import create_extraction_run

    return create_extraction_run(
        db_session,
        website_id=website.id,
        configuration_version=4,
        pattern_name="json_ld_event",
        run_type="preview",
        status="success",
        source_url=LISTING_URL,
        final_url=LISTING_URL,
        started_at=datetime.now(UTC),
    )


def test_recording_a_decision_is_append_only(db_session, base_policy, website, preview_run):
    denied = _evaluate(base_policy, city_is_active=False, preview_run_id=preview_run.id)
    first = record_decision(db_session, denied, website=website)
    allowed = _evaluate(base_policy, preview_run_id=preview_run.id)
    second = record_decision(
        db_session, allowed, website=website, reevaluates_decision_id=first.id
    )
    db_session.refresh(first)

    assert first.final_decision == AUTOMATIC_APPROVAL_DENIED
    assert second.final_decision == AUTOMATIC_APPROVAL_ALLOWED
    assert second.reevaluates_decision_id == first.id
    assert first.reasons_failed, "the original denial reasons must survive"
    assert len(list_decisions_for_website(db_session, website.id)) == 2


def test_recording_a_decision_writes_a_system_audit_entry(
    db_session, base_policy, website, preview_run
):
    record_decision(
        db_session, _evaluate(base_policy, preview_run_id=preview_run.id), website=website
    )
    entry = (
        db_session.query(AuditLog)
        .filter(AuditLog.action == "auto_onboarding_decision_created")
        .one()
    )
    assert entry.actor_type == ACTOR_SYSTEM
    assert entry.user_id is None


def test_a_denied_decision_is_recorded_too(db_session, base_policy, website, preview_run):
    denied = _evaluate(base_policy, website_is_archived=True, preview_run_id=preview_run.id)
    decision = record_decision(db_session, denied, website=website)
    assert decision.final_decision == AUTOMATIC_APPROVAL_DENIED
    assert decision.eligible_for_automatic_approval is False
    assert any("archived" in reason for reason in decision.reasons_failed)
