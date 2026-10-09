"""Regression: onboarding/quality displays render historical & partial snapshots.

Preview quality is persisted as JSON; older snapshots lack the Phase 8G
date-range/geographic keys. The onboarding-result page (and related quality
displays) must render 200 for any shape, distinguishing an absent metric from
an evaluated zero.
"""

from __future__ import annotations

import copy

import pytest

from app.core.onboarding_jobs import NEEDS_REVIEW, READY_FOR_APPROVAL, UNSUPPORTED
from app.models.onboarding_batch import OnboardingBatch
from app.models.onboarding_job import OnboardingJob
from app.repositories.auto_onboarding import create_decision
from app.services.quality_presentation import format_percent, quality_view

# Core metrics present in every snapshot ever written (pre- and post-8G).
_CORE = {
    "candidates_found": 6, "valid_count": 6, "rejected_count": 0,
    "valid_percentage": 1.0, "rejected_percentage": 0.0,
    "required_field_coverage": {"title": 1.0, "start_date": 1.0},
    "date_parse_success_rate": 1.0, "url_validity_rate": 1.0, "duplicate_rate": 0.0,
    "warning_count": 0, "pagination_truncated": False, "detail_fetch_used": False,
    "pages_fetched": 1,
}
_RANGE_FULL = {
    "range_count": 2, "range_parse_success_rate": 0.95, "end_date_success_rate": 0.8,
    "ambiguous_range_rejections": 1,
    "geographic_considered": 3, "geographic_included": 2, "geographic_excluded": 1,
    "geographic_missing": 0, "geographic_inclusion_rate": 0.66,
}


def _quality(**over):
    q = dict(_CORE)
    q.update(over)
    return q


# --- view-model unit tests ---------------------------------------------------


def test_quality_view_of_none_is_none():
    assert quality_view(None) is None
    assert quality_view({}) is None


def test_historical_snapshot_has_no_range_metrics():
    v = quality_view(_quality())  # core only, no range/geo keys
    assert v.range_parse_success_rate is None
    assert v.range_candidate_count is None
    assert v.range_applicable is False
    assert v.range_metrics_recorded is False
    assert v.geographic_configured is False


def test_zero_range_activity_is_not_applicable():
    v = quality_view(_quality(range_count=0, range_parse_success_rate=1.0,
                              ambiguous_range_rejections=0))
    assert v.range_applicable is False


def test_range_present_but_metric_absent_is_recorded_false():
    v = quality_view(_quality(range_count=2))  # candidates but no success rate
    assert v.range_applicable is True
    assert v.range_metrics_recorded is False


def test_explicit_zero_is_distinct_from_absent():
    v = quality_view(_quality(range_count=2, range_parse_success_rate=0.0))
    assert v.range_applicable is True
    assert v.range_metrics_recorded is True
    assert v.range_parse_success_rate == 0.0


def test_quality_view_accepts_an_object():
    from app.extraction.inference.quality import evaluate_preview_quality
    from app.schemas.extraction import SiteConfiguration

    q = evaluate_preview_quality(
        [], SiteConfiguration(pattern_name="json_ld_event", listing_url="https://e/x"),
        warnings=[], pages_fetched=1, website_id=1, city_id=None,
    )
    v = quality_view(q)
    assert v is not None
    assert v.range_parse_success_rate == q.range_parse_success_rate


def test_format_percent_absent_vs_zero():
    assert format_percent(None) == "—"
    assert format_percent("nope") == "—"
    assert format_percent(0.0) == "0%"
    assert format_percent(0.95) == "95%"


# --- page integration tests --------------------------------------------------


@pytest.fixture
def admin_client(client, make_super_admin, login):
    make_super_admin(email="quality-root@example.com", password="root-pass-1234")
    login("quality-root@example.com", "root-pass-1234")
    return client


def _website_with_quality(db_session, make_city, make_website, quality, *,
                          outcome="ready_for_approval", status=READY_FOR_APPROVAL):
    city = make_city()
    website = make_website(city, approved_pattern=None)
    website.onboarding_status = status
    website.proposed_pattern = {
        "inference": {
            "outcome": outcome,
            "generated_at": "2026-01-01T00:00:00+00:00",
            "inference": {
                "pattern_name": "generic_html_cards",
                "detection_confidence": 0.9, "proposal_confidence": 0.9,
                "field_candidates": [], "date_format_candidates": [],
                "missing_required_fields": [], "warnings": [], "error": None,
                "configuration": None,
            },
            "quality": copy.deepcopy(quality),
            "samples": None,
            "blocking_reasons": [],
        }
    }
    db_session.commit()
    db_session.refresh(website)
    return website


def _get(admin_client, website):
    return admin_client.get(f"/admin/websites/{website.id}/onboarding")


def test_historical_dict_without_range_keys_returns_200(
    admin_client, make_city, make_website, db_session
):
    website = _website_with_quality(db_session, make_city, make_website, _quality())
    resp = _get(admin_client, website)
    assert resp.status_code == 200
    assert "not applicable" in resp.text  # range section, no crash


def test_no_range_preview_renders_not_applicable(
    admin_client, make_city, make_website, db_session
):
    website = _website_with_quality(
        db_session, make_city, make_website,
        _quality(range_count=0, range_parse_success_rate=1.0, ambiguous_range_rejections=0),
    )
    resp = _get(admin_client, website)
    assert resp.status_code == 200
    assert "not applicable" in resp.text


def test_range_candidates_missing_metric_renders_not_recorded(
    admin_client, make_city, make_website, db_session
):
    website = _website_with_quality(
        db_session, make_city, make_website, _quality(range_count=2)
    )
    resp = _get(admin_client, website)
    assert resp.status_code == 200
    assert "not recorded" in resp.text


def test_range_success_zero_renders_0_percent(
    admin_client, make_city, make_website, db_session
):
    website = _website_with_quality(
        db_session, make_city, make_website,
        _quality(range_count=2, range_parse_success_rate=0.0, end_date_success_rate=0.0),
    )
    resp = _get(admin_client, website)
    assert resp.status_code == 200
    assert "parse success 0%" in resp.text


def test_range_success_nonzero_renders_percentage(
    admin_client, make_city, make_website, db_session
):
    website = _website_with_quality(
        db_session, make_city, make_website,
        _quality(range_count=2, range_parse_success_rate=0.95, end_date_success_rate=0.8),
    )
    resp = _get(admin_client, website)
    assert resp.status_code == 200
    assert "parse success 95%" in resp.text


def test_current_full_quality_page_renders(
    admin_client, make_city, make_website, db_session
):
    website = _website_with_quality(
        db_session, make_city, make_website, _quality(**_RANGE_FULL)
    )
    resp = _get(admin_client, website)
    assert resp.status_code == 200
    assert "multi-day" in resp.text
    assert "Geographic filter" in resp.text


def test_failed_preview_with_sparse_metrics_returns_200(
    admin_client, make_city, make_website, db_session
):
    website = _website_with_quality(
        db_session, make_city, make_website,
        {"candidates_found": 0, "valid_count": 0},  # very sparse / partial
        outcome="failed", status=UNSUPPORTED,
    )
    resp = _get(admin_client, website)
    assert resp.status_code == 200


def test_needs_review_website_returns_200(
    admin_client, make_city, make_website, db_session
):
    website = _website_with_quality(
        db_session, make_city, make_website, _quality(),
        outcome="needs_review", status=NEEDS_REVIEW,
    )
    assert _get(admin_client, website).status_code == 200


def test_ready_for_approval_website_returns_200(
    admin_client, make_city, make_website, db_session
):
    website = _website_with_quality(
        db_session, make_city, make_website, _quality(**_RANGE_FULL)
    )
    assert _get(admin_client, website).status_code == 200


# --- related templates: job detail & decision detail with sparse snapshots ---


def test_job_detail_renders_sparse_quality(
    admin_client, make_city, make_website, db_session
):
    city = make_city()
    batch = OnboardingBatch(default_city_id=city.id)
    db_session.add(batch)
    db_session.commit()
    job = OnboardingJob(
        batch_id=batch.id, row_number=1, submitted_url="https://x/e",
        normalized_url="https://x/e", status=READY_FOR_APPROVAL,
        # Historical/partial quality: missing several rate keys.
        quality={"valid_percentage": 1.0, "warning_count": 0, "pages_fetched": 1},
    )
    db_session.add(job)
    db_session.commit()
    resp = admin_client.get(f"/admin/onboarding/jobs/{job.id}")
    assert resp.status_code == 200


def test_decision_detail_renders_sparse_metrics_snapshot(
    admin_client, make_city, make_website, db_session
):
    city = make_city()
    website = make_website(city)
    decision = create_decision(
        db_session, website_id=website.id, final_decision="automatic_approval_denied",
        metrics_snapshot={"valid_percentage": 0.9},  # no range keys
        thresholds_snapshot={},
    )
    resp = admin_client.get(f"/admin/onboarding/decisions/{decision.id}")
    assert resp.status_code == 200
