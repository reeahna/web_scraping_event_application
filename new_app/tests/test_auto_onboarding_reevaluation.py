"""Re-evaluating a website under the current policy."""

from __future__ import annotations

import asyncio
import itertools

import httpx
import pytest

from app.core.exceptions import AppError
from app.models.auto_onboarding_policy import AutoOnboardingPolicy, AutoOnboardingPolicyCity
from app.models.extraction_run import ExtractionRun
from app.repositories.auto_onboarding import list_decisions_for_website
from app.services.auto_onboarding_reevaluation import reevaluate_website
from app.services.onboarding_automation import detect_and_configure
from tests.extraction_helpers import load_fixture, patched_http_fetch

LISTING_URL = "https://venue.example.org/events"
_names = itertools.count(1)


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
    return make_city(name="Reeval City", slug="reeval-city", timezone="UTC")


@pytest.fixture
def website(db_session, city, make_website):
    site = make_website(city, name="Reeval Site", base_url="https://venue.example.org")
    site.event_listing_url = LISTING_URL
    db_session.commit()
    return site


@pytest.fixture
def make_policy(db_session, city):
    def _make(**overrides):
        policy = AutoOnboardingPolicy(
            name=f"Reeval policy {next(_names)}",
            active=True,
            automatic_approval_enabled=True,
            allowed_pattern_names=["json_ld_event"],
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
        db_session.add(AutoOnboardingPolicyCity(policy_id=policy.id, city_id=city.id))
        db_session.commit()
        db_session.refresh(policy)
        return policy

    return _make


def _configure(db_session, website, listing_body):
    with patched_http_fetch(_handler(listing_body)):
        return asyncio.run(detect_and_configure(db_session, website))


def _reevaluate(db_session, website, listing_body):
    with patched_http_fetch(_handler(listing_body)):
        return asyncio.run(reevaluate_website(db_session, website))


def test_reevaluation_appends_a_decision_linked_to_the_previous_one(
    db_session, website, listing_body
):
    _configure(db_session, website, listing_body)
    first = list_decisions_for_website(db_session, website.id)[0]
    first_reasons = list(first.reasons_failed)

    result = _reevaluate(db_session, website, listing_body)
    db_session.refresh(first)

    assert result.decision.id != first.id
    assert result.decision.reevaluates_decision_id == first.id
    assert result.decision.decision_kind == "reevaluation"
    assert first.reasons_failed == first_reasons, "the prior decision is unchanged"
    assert len(list_decisions_for_website(db_session, website.id)) == 2


def test_a_matching_preview_is_reused_rather_than_rerun(db_session, website, listing_body):
    _configure(db_session, website, listing_body)
    before = db_session.query(ExtractionRun).filter(
        ExtractionRun.website_id == website.id, ExtractionRun.run_type == "preview"
    ).count()

    result = _reevaluate(db_session, website, listing_body)
    after = db_session.query(ExtractionRun).filter(
        ExtractionRun.website_id == website.id, ExtractionRun.run_type == "preview"
    ).count()

    assert result.preview_reused is True
    assert after == before, "a current preview must not be re-run"


def test_a_stale_preview_triggers_a_fresh_one(db_session, website, listing_body):
    from app.schemas.extraction import SiteConfiguration
    from app.services.website_configuration import save_draft_configuration

    _configure(db_session, website, listing_body)
    # Saving the draft again bumps the version, making the preview stale.
    save_draft_configuration(
        db_session, website, SiteConfiguration.model_validate(website.configuration)
    )
    before = db_session.query(ExtractionRun).filter(
        ExtractionRun.website_id == website.id, ExtractionRun.run_type == "preview"
    ).count()

    result = _reevaluate(db_session, website, listing_body)
    after = db_session.query(ExtractionRun).filter(
        ExtractionRun.website_id == website.id, ExtractionRun.run_type == "preview"
    ).count()

    assert result.preview_reused is False
    assert after == before + 1, "a stale preview is never reused"


def test_reevaluation_uses_the_current_policy_version(
    db_session, website, listing_body, make_policy
):
    policy = make_policy()
    _configure(db_session, website, listing_body)
    policy.minimum_valid_events = 2
    policy.version += 1
    db_session.commit()

    result = _reevaluate(db_session, website, listing_body)
    assert result.decision.policy_version == policy.version


def test_a_stricter_policy_never_deactivates_a_live_website(
    db_session, website, listing_body, make_policy
):
    policy = make_policy(automatic_activation_enabled=True)
    _configure(db_session, website, listing_body)
    db_session.refresh(website)
    assert website.is_active is True

    # Make the policy unsatisfiable, then re-evaluate.
    policy.minimum_valid_events = 9999
    policy.version += 1
    db_session.commit()

    result = _reevaluate(db_session, website, listing_body)
    db_session.refresh(website)

    assert result.decision.eligible_for_automatic_approval is False
    # A stricter policy recommends review; it never shuts a live source off.
    assert website.is_active is True
    assert website.onboarding_status == "active"


def test_an_archived_website_cannot_be_reevaluated(db_session, website, listing_body):
    from datetime import UTC, datetime

    _configure(db_session, website, listing_body)
    website.archived_at = datetime.now(UTC)
    website.onboarding_status = "archived"
    db_session.commit()

    with pytest.raises(AppError):
        _reevaluate(db_session, website, listing_body)


def test_a_website_without_a_draft_cannot_be_reevaluated(db_session, city, make_website):
    site = make_website(city, name="No Draft", base_url="https://nodraft.example.org")
    with pytest.raises(AppError):
        asyncio.run(reevaluate_website(db_session, site))
