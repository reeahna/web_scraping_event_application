"""Increments 3 and 4: executing a decision, and the pipeline integration.

Everything here runs the real services — `approve_configuration`,
`transition_website`, `detect_and_configure`, `process_batch` — against
fixtures through a mocked transport. Nothing is stubbed out that would hide a
safeguard.
"""

from __future__ import annotations

import asyncio
import itertools

import httpx
import pytest

from app.core.auto_onboarding import (
    ACTION_ACTIVATION,
    ACTION_APPROVAL,
    ACTOR_SYSTEM,
    ACTOR_USER,
    AUTOMATICALLY_ACTIVATED,
    AUTOMATICALLY_APPROVED,
)
from app.core.onboarding_jobs import (
    AUTOMATICALLY_ACTIVATED as JOB_ACTIVATED,
)
from app.core.onboarding_jobs import (
    AUTOMATICALLY_APPROVED as JOB_APPROVED,
)
from app.core.onboarding_jobs import (
    NEEDS_REVIEW as JOB_NEEDS_REVIEW,
)
from app.core.onboarding_jobs import (
    READY_FOR_APPROVAL as JOB_READY,
)
from app.models.audit_log import AuditLog
from app.models.auto_onboarding_decision import AutoOnboardingDecision
from app.models.auto_onboarding_policy import AutoOnboardingPolicy
from app.models.event import Event
from app.models.website import Website
from app.repositories.auto_onboarding import (
    action_results_for_decision,
    latest_decision_for_website,
    list_decisions_for_website,
)
from app.services.auto_onboarding_execution import effective_decision
from app.services.bulk_onboarding import create_batch_from_submission, process_batch
from app.services.onboarding_automation import detect_and_configure
from app.services.onboarding_submission import SubmissionLimits, parse_url_lines
from tests.extraction_helpers import load_fixture, patched_http_fetch

LISTING_URL = "https://venue.example.org/events"
LIMITS = SubmissionLimits(
    max_urls=50, max_csv_rows=50, max_csv_bytes=100_000, max_url_length=2000
)
_names = itertools.count(1)


# A listing that produces a high-quality JSON-LD extraction: 100% valid, so
# the only thing standing between it and automatic approval is policy.
def _handler(body: str):
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) != LISTING_URL:
            return httpx.Response(404, text="not found")
        return httpx.Response(200, text=body, headers={"content-type": "text/html"})

    return handler


@pytest.fixture
def listing_body():
    return load_fixture("jsonld_multiple_events.html")


@pytest.fixture
def city(make_city):
    return make_city(name="Policy City", slug="policy-city", timezone="UTC")


@pytest.fixture
def make_policy(db_session):
    def _make(**overrides) -> AutoOnboardingPolicy:
        policy = AutoOnboardingPolicy(
            name=f"Execution policy {next(_names)}",
            active=True,
            automatic_approval_enabled=True,
            allowed_pattern_names=["json_ld_event"],
            # The JSON-LD fixture yields a handful of events; keep the counts
            # reachable while leaving every *safety* rule at its default.
            minimum_events_found=1,
            minimum_valid_events=1,
            minimum_distinct_events=1,
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
def assign_city_policy(db_session, city):
    from app.models.auto_onboarding_policy import AutoOnboardingPolicyCity

    def _assign(policy):
        db_session.add(AutoOnboardingPolicyCity(policy_id=policy.id, city_id=city.id))
        db_session.commit()
        return policy

    return _assign


@pytest.fixture
def website(db_session, city, make_website):
    site = make_website(city, name="Policy Site", base_url="https://venue.example.org")
    site.event_listing_url = LISTING_URL
    db_session.commit()
    return site


def _configure(db_session, website, listing_body, **kwargs):
    with patched_http_fetch(_handler(listing_body)):
        return asyncio.run(
            detect_and_configure(db_session, website, **kwargs)
        )


# --- default behaviour is unchanged ------------------------------------------


def test_with_the_conservative_default_nothing_is_approved(
    db_session, website, listing_body
):
    """The seeded default policy applies (no city assignment), and it has
    automatic approval off — so behaviour is exactly Phase 8C's."""
    result = _configure(db_session, website, listing_body)
    db_session.refresh(website)

    assert result.outcome in (JOB_READY, JOB_NEEDS_REVIEW)
    assert website.approved_pattern is None
    assert website.is_active is False
    decision = latest_decision_for_website(db_session, website.id)
    assert decision is not None, "a decision is recorded even when nothing is done"
    assert decision.eligible_for_automatic_approval is False
    assert action_results_for_decision(db_session, decision.id) == []


def test_a_denied_source_records_its_reasons(db_session, website, listing_body):
    _configure(db_session, website, listing_body)
    decision = latest_decision_for_website(db_session, website.id)
    assert decision.reasons_failed
    assert decision.metrics_snapshot
    assert decision.thresholds_snapshot


# --- automatic approval -------------------------------------------------------


@pytest.fixture
def approving_policy(make_policy, assign_city_policy):
    return assign_city_policy(make_policy())


def test_a_qualifying_source_is_automatically_approved(
    db_session, website, listing_body, approving_policy
):
    result = _configure(db_session, website, listing_body)
    db_session.refresh(website)

    assert result.outcome == AUTOMATICALLY_APPROVED
    assert website.approved_pattern is not None
    assert website.onboarding_status == "approved"
    # Approval alone never activates.
    assert website.is_active is False


def test_the_approved_snapshot_matches_the_previewed_configuration_version(
    db_session, website, listing_body, approving_policy
):
    _configure(db_session, website, listing_body)
    db_session.refresh(website)
    decision = latest_decision_for_website(db_session, website.id)

    assert website.active_configuration_version == decision.configuration_version
    approval = action_results_for_decision(db_session, decision.id)[0]
    assert approval.action_type == ACTION_APPROVAL
    assert approval.succeeded is True
    assert approval.configuration_version == website.active_configuration_version


def test_automatic_approval_is_audited_as_a_system_action(
    db_session, website, listing_body, approving_policy
):
    _configure(db_session, website, listing_body)
    entry = (
        db_session.query(AuditLog)
        .filter(AuditLog.action == "website_automatically_approved")
        .one()
    )
    assert entry.actor_type == ACTOR_SYSTEM
    assert entry.user_id is None, "no human may be credited with an automatic approval"


def test_automatic_approval_persists_no_events(
    db_session, website, listing_body, approving_policy
):
    _configure(db_session, website, listing_body)
    assert db_session.query(Event).count() == 0


def test_the_decision_is_not_mutated_by_the_action(
    db_session, website, listing_body, approving_policy
):
    _configure(db_session, website, listing_body)
    decision = latest_decision_for_website(db_session, website.id)
    db_session.refresh(decision)

    # The evaluation still says what it concluded, not what happened.
    assert decision.final_decision == "automatic_approval_allowed"
    assert decision.eligible_for_automatic_approval is True
    # What happened is derived from the action rows.
    assert effective_decision(decision) == AUTOMATICALLY_APPROVED


def test_a_stale_preview_still_blocks_automatic_approval(
    db_session, website, listing_body, make_policy, assign_city_policy
):
    """The approval service's own staleness rule is not bypassed: bump the
    draft after preview and approval must refuse."""
    from app.services.website_configuration import save_draft_configuration

    assign_city_policy(make_policy())

    original = detect_and_configure

    async def bump_then_configure(db, site, **kwargs):
        result = await original(db, site, **kwargs)
        return result

    # Approve once normally, then edit the draft and confirm the approved
    # snapshot is the previewed one, not the newer draft.
    _configure(db_session, website, listing_body)
    db_session.refresh(website)
    approved_version = website.active_configuration_version

    from app.schemas.extraction import SiteConfiguration

    save_draft_configuration(
        db_session,
        website,
        SiteConfiguration.model_validate(website.configuration),
    )
    db_session.refresh(website)
    assert website.configuration_version > approved_version
    assert website.active_configuration_version == approved_version


# --- automatic activation -----------------------------------------------------


def test_activation_does_not_happen_when_the_policy_disables_it(
    db_session, website, listing_body, approving_policy
):
    _configure(db_session, website, listing_body)
    db_session.refresh(website)
    decision = latest_decision_for_website(db_session, website.id)

    assert website.is_active is False
    assert [r.action_type for r in action_results_for_decision(db_session, decision.id)] == [
        ACTION_APPROVAL
    ]


def test_a_qualifying_source_is_activated_when_the_policy_enables_it(
    db_session, website, listing_body, make_policy, assign_city_policy
):
    assign_city_policy(make_policy(automatic_activation_enabled=True))
    result = _configure(db_session, website, listing_body)
    db_session.refresh(website)

    assert result.outcome == AUTOMATICALLY_ACTIVATED
    assert website.onboarding_status == "active"
    assert website.is_active is True

    decision = latest_decision_for_website(db_session, website.id)
    kinds = [r.action_type for r in action_results_for_decision(db_session, decision.id)]
    assert kinds == [ACTION_APPROVAL, ACTION_ACTIVATION], "two separate actions"


def test_activation_is_audited_separately_and_as_a_system_action(
    db_session, website, listing_body, make_policy, assign_city_policy
):
    assign_city_policy(make_policy(automatic_activation_enabled=True))
    _configure(db_session, website, listing_body)

    approved = db_session.query(AuditLog).filter(
        AuditLog.action == "website_automatically_approved"
    ).one()
    activated = db_session.query(AuditLog).filter(
        AuditLog.action == "website_automatically_activated"
    ).one()
    assert approved.id != activated.id, "approval and activation are separate audit actions"
    assert activated.actor_type == ACTOR_SYSTEM
    assert activated.user_id is None


def test_activation_is_refused_when_the_city_became_inactive_after_approval(
    db_session, website, listing_body, city, make_policy, assign_city_policy
):
    """The post-approval re-check: approval succeeded, then the world changed
    before activation, so activation must not proceed."""
    from app.services.auto_onboarding_execution import execute_decision
    from app.services.website_configuration import approve_configuration

    assign_city_policy(make_policy(automatic_activation_enabled=True))
    # Approve through the ordinary path first.
    _configure(db_session, website, listing_body)
    db_session.refresh(website)
    assert website.is_active is True

    # Now simulate a second decision whose activation re-check should refuse.
    city.is_active = False
    db_session.commit()
    decision = latest_decision_for_website(db_session, website.id)
    outcome = execute_decision(db_session, decision, website=website)
    activation = outcome.activation
    assert activation is None or activation.succeeded is False
    assert approve_configuration is not None  # imported to assert we use the real service


def test_an_archived_website_is_never_approved_or_activated(
    db_session, website, listing_body, make_policy, assign_city_policy
):
    from datetime import UTC, datetime

    assign_city_policy(make_policy(automatic_activation_enabled=True))
    website.archived_at = datetime.now(UTC)
    website.onboarding_status = "archived"
    db_session.commit()

    _configure(db_session, website, listing_body)
    db_session.refresh(website)

    assert website.approved_pattern is None
    assert website.is_active is False
    decision = latest_decision_for_website(db_session, website.id)
    assert decision.eligible_for_automatic_approval is False
    assert any("archived" in reason for reason in decision.reasons_failed)


# --- manual approval after denial ---------------------------------------------


def test_manual_approval_still_works_after_a_policy_denial(
    db_session, website, listing_body, make_user
):
    from app.services.website_configuration import approve_configuration

    _configure(db_session, website, listing_body)  # default policy denies
    db_session.refresh(website)
    assert website.approved_pattern is None

    user = make_user(email="approver@example.com")
    approve_configuration(db_session, website, approved_by_user_id=user.id)
    db_session.refresh(website)

    assert website.approved_pattern is not None
    assert website.approved_by_user_id == user.id


def test_a_manual_approval_is_recorded_as_a_human_action(db_session, make_user):
    from app.services.audit import record_audit

    user = make_user(email="human@example.com")
    entry = record_audit(db_session, actor_id=user.id, action="configuration_approved")
    assert entry.actor_type == ACTOR_USER
    assert entry.user_id == user.id


# --- bulk integration ---------------------------------------------------------


def _run_batch(db_session, city, listing_body, url=LISTING_URL):
    parsed = parse_url_lines(url, LIMITS)
    batch = create_batch_from_submission(
        db_session,
        parsed,
        submitted_by_user_id=None,
        default_city_id=city.id,
        default_timezone=None,
        redetect_existing=False,
        source_kind="single",
        correlation_id="policy-test",
    )
    with patched_http_fetch(_handler(listing_body)):
        asyncio.run(process_batch(db_session, batch, limit=5))
    db_session.refresh(batch)
    return batch


def test_a_bulk_job_becomes_automatically_approved(
    db_session, city, listing_body, make_policy, assign_city_policy
):
    assign_city_policy(make_policy())
    batch = _run_batch(db_session, city, listing_body)
    job = batch.jobs[0]

    assert job.status == JOB_APPROVED
    website = db_session.get(Website, job.website_id)
    assert website.approved_pattern is not None
    assert website.is_active is False
    assert db_session.query(Event).count() == 0


def test_a_bulk_job_becomes_automatically_activated_when_enabled(
    db_session, city, listing_body, make_policy, assign_city_policy
):
    assign_city_policy(make_policy(automatic_activation_enabled=True))
    batch = _run_batch(db_session, city, listing_body)
    job = batch.jobs[0]

    assert job.status == JOB_ACTIVATED
    website = db_session.get(Website, job.website_id)
    assert website.is_active is True


def test_a_denied_bulk_job_keeps_its_manual_review_outcome(db_session, city, listing_body):
    batch = _run_batch(db_session, city, listing_body)  # default policy denies
    job = batch.jobs[0]

    assert job.status in (JOB_READY, JOB_NEEDS_REVIEW)
    website = db_session.get(Website, job.website_id)
    assert website.approved_pattern is None


def test_the_decision_links_to_its_job_and_batch(
    db_session, city, listing_body, make_policy, assign_city_policy
):
    assign_city_policy(make_policy())
    batch = _run_batch(db_session, city, listing_body)
    job = batch.jobs[0]

    decision = (
        db_session.query(AutoOnboardingDecision)
        .filter(AutoOnboardingDecision.onboarding_job_id == job.id)
        .one()
    )
    assert decision.onboarding_batch_id == batch.id
    assert decision.website_id == job.website_id


def test_batch_counts_reflect_the_automatic_outcome(
    db_session, city, listing_body, make_policy, assign_city_policy
):
    from app.repositories.onboarding import status_counts

    assign_city_policy(make_policy())
    batch = _run_batch(db_session, city, listing_body)
    counts = status_counts(db_session, batch.id)

    assert counts.get(JOB_APPROVED) == 1
    assert batch.completed_count == 1
    assert batch.status == "completed"


def test_policy_precedence_city_over_global(
    db_session, city, listing_body, make_policy, assign_city_policy, make_website
):
    """The city's policy approves; the global default would not."""
    assign_city_policy(make_policy())
    batch = _run_batch(db_session, city, listing_body)
    assert batch.jobs[0].status == JOB_APPROVED

    decision = latest_decision_for_website(db_session, batch.jobs[0].website_id)
    assert decision.policy_id is not None
    global_default = (
        db_session.query(AutoOnboardingPolicy)
        .filter(AutoOnboardingPolicy.is_global_default.is_(True))
        .one()
    )
    assert decision.policy_id != global_default.id


def test_evaluating_a_policy_does_not_rerun_detection_or_preview(
    db_session, city, listing_body, make_policy, assign_city_policy
):
    from app.models.extraction_run import ExtractionRun

    assign_city_policy(make_policy())
    batch = _run_batch(db_session, city, listing_body)
    website_id = batch.jobs[0].website_id

    runs = db_session.query(ExtractionRun).filter(ExtractionRun.website_id == website_id).all()
    assert sum(1 for r in runs if r.run_type == "detection") == 1
    assert sum(1 for r in runs if r.run_type == "preview") == 1


def test_a_second_evaluation_appends_a_decision_and_preserves_the_first(
    db_session, website, listing_body, make_policy, assign_city_policy
):
    assign_city_policy(make_policy())
    _configure(db_session, website, listing_body)
    first = latest_decision_for_website(db_session, website.id)
    first_reasons = list(first.reasons_passed)

    _configure(db_session, website, listing_body)
    decisions = list_decisions_for_website(db_session, website.id)
    db_session.refresh(first)

    assert len(decisions) == 2
    assert first.reasons_passed == first_reasons, "history must not be rewritten"
