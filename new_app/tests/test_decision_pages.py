"""The decision pages answer "what happened and why".

The verdict used to be the fourth row of a thirteen-row table, weighted the same
as "Kind: initial", and the metrics table gave observed values beside thresholds
with no indication of which comparisons had actually failed.
"""

import re

import pytest

from app.models.auto_onboarding_action_result import AutoOnboardingActionResult
from app.models.auto_onboarding_decision import AutoOnboardingDecision
from app.models.auto_onboarding_policy import AutoOnboardingPolicy


@pytest.fixture
def decision(db_session, make_city, make_website, make_super_admin, login):
    user = make_super_admin(email="dec@example.com", password="dec-pass-1234")
    website = make_website(make_city(name="Dec City", slug="dec-city"), name="Dec Source")
    db_session.commit()
    policy = db_session.query(AutoOnboardingPolicy).first()
    record = AutoOnboardingDecision(
        website_id=website.id,
        policy_id=policy.id,
        policy_version=policy.version,
        decision_kind="initial",
        final_decision="not_eligible",
        eligible_for_automatic_approval=False,
        eligible_for_automatic_activation=False,
        activation_policy_enabled=False,
        detected_pattern="the_events_calendar",
        detector_confidence=0.72,
        system_actor_type="system",
        submitted_by_user_id=user.id,
        # One metric under a minimum, one over a maximum, one with no bound at
        # all: the three cases the status column has to tell apart.
        metrics_snapshot={"detector_confidence": 0.72, "events_found": 14, "unbounded_metric": 5},
        thresholds_snapshot={"minimum_detector_confidence": 0.80, "maximum_events_found": 10},
        reasons_passed=["a rule that passed"],
        reasons_failed=["detector confidence 0.72 is below the minimum of 0.80"],
    )
    db_session.add(record)
    db_session.commit()
    db_session.add(
        AutoOnboardingActionResult(
            decision_id=record.id,
            website_id=website.id,
            action_type="approve",
            attempted=False,
            succeeded=False,
            actor_type="system",
        )
    )
    db_session.commit()
    login("dec@example.com", "dec-pass-1234")
    return record


@pytest.fixture
def detail_html(client, decision):
    response = client.get(f"/admin/onboarding/decisions/{decision.id}")
    assert response.status_code == 200
    return response.text


def test_the_verdict_comes_before_the_supporting_detail(detail_html):
    assert 'class="verdict' in detail_html
    assert detail_html.index('class="verdict') < detail_html.index("What was evaluated")


def test_a_failed_decision_reads_as_failed(detail_html):
    assert "verdict-bad" in detail_html
    assert "1 rule failed" in detail_html
    assert "detector confidence 0.72 is below the minimum of 0.80" in detail_html


def test_metrics_carry_a_pass_or_fail_status(detail_html):
    """Observed beside a threshold left the reader comparing 0.72 against
    "at least 0.8" by eye, for every row."""
    assert ">Status<" in detail_html
    assert ">Fail<" in detail_html


def test_an_unbounded_metric_is_not_reported_as_a_pass(detail_html):
    """A metric the policy sets no bound for has not passed anything."""
    assert "not bounded" in detail_html


def test_not_attempted_is_distinct_from_attempted_and_failed(detail_html):
    """Two Yes/No columns made the reader combine them to tell these apart."""
    assert "Not attempted" in detail_html


def test_timestamps_are_formatted_not_raw(detail_html):
    """Raw values rendered as "2026-09-16 19:05:05.971911"."""
    assert not re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+", detail_html)
    assert " at " in detail_html


def test_passed_rules_are_available_but_not_competing_with_the_failures(detail_html):
    assert "Rules that passed (1)" in detail_html
    assert detail_html.index("reason-list-failed") < detail_html.index("reason-list-passed")


def test_history_badges_the_outcome(client, decision):
    response = client.get(f"/admin/websites/{decision.website_id}/decisions")
    assert response.status_code == 200
    assert "badge-danger" in response.text
    assert not re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+", response.text)


def test_policy_page_uses_the_same_outcome_badge(client, decision):
    response = client.get(f"/admin/settings/onboarding-policies/{decision.policy_id}")
    assert response.status_code == 200
    assert "badge-danger" in response.text
