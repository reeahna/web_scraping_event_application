"""Router + template behaviour for the unsupported-source recovery UI:
registry-driven selector, honest detector explanation, and the permission- and
CSRF-gated restricted-browser retry action.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.config import get_settings
from app.core.permissions import REGISTERED_USER
from app.models.audit_log import AuditLog
from app.services.browser_recovery import BrowserRecoveryResult

STALE_SENTENCE = "wordpress_rest &gt; json_ld_event &gt; generic_html_cards"


def _csrf(client) -> str:
    return client.cookies.get("csrf_token")


def _detection(all_results: dict | None, *, winner=None, pattern_name=None) -> dict:
    return {
        "pattern_name": pattern_name,
        "confidence": 0.0 if pattern_name is None else 0.9,
        "evidence": {"all_results": all_results or {}, "winner": winner},
        "discovered_endpoints": [],
        "browser_required": False,
        "warnings": [],
        "detector_version": "1",
        "detected_at": "2026-07-30T00:00:00+00:00",
    }


@pytest.fixture
def unsupported_website(db_session, make_city, make_website):
    def _make(all_results: dict | None = None, *, winner=None, timezone_override=None):
        city = make_city()
        website = make_website(city)
        website.onboarding_status = "unsupported"
        website.event_listing_url = "https://example.com/events"
        website.timezone_override = timezone_override
        website.proposed_pattern = {
            "detection": _detection(all_results, winner=winner),
            "configuration": None,
        }
        db_session.add(website)
        db_session.commit()
        db_session.refresh(website)
        return website

    return _make


@pytest.fixture
def enable_browser(monkeypatch):
    monkeypatch.setattr(get_settings(), "browser_extraction_enabled", True, raising=False)


@pytest.fixture
def disable_browser(monkeypatch):
    # Explicit, so the test is hermetic regardless of any local .env default.
    monkeypatch.setattr(get_settings(), "browser_extraction_enabled", False, raising=False)


# --- registry-driven selector + detector explanation ------------------------


def test_detail_page_selector_is_registry_driven_without_stale_sentence(
    client, make_super_admin, unsupported_website, login
):
    make_super_admin(email="rec-admin@example.com", password="root-pass-1234")
    website = unsupported_website({"wordpress_rest": {"confidence": 0.1}})
    login("rec-admin@example.com", "root-pass-1234")

    resp = client.get(f"/admin/websites/{website.id}")
    assert resp.status_code == 200
    body = resp.text
    # Stale hardcoded reliability sentence is gone.
    assert STALE_SENTENCE not in body
    # Registry-driven ordering is shown instead.
    assert "Registry reliability order" in body
    # Every registered pattern appears in the advanced selector.
    for name in ("wordpress_rest", "json_ld_event", "generic_html_cards", "algolia_search"):
        assert name in body
    # Not preselected: a disabled placeholder option is present.
    assert "Choose a pattern…" in body
    # Advanced framing for the manual fallback.
    assert "manually select an extraction pattern" in body


def test_detector_explanation_is_honest_with_no_evidence(
    client, make_super_admin, unsupported_website, login
):
    make_super_admin(email="rec-admin2@example.com", password="root-pass-1234")
    website = unsupported_website({})  # zero detector results recorded
    login("rec-admin2@example.com", "root-pass-1234")

    body = client.get(f"/admin/websites/{website.id}").text
    assert "No detector evidence was recorded for this run." in body
    # No fictional winner or tie-break story.
    assert "No detector qualified" in body
    assert STALE_SENTENCE not in body


def test_detector_results_render_when_present(
    client, make_super_admin, unsupported_website, login
):
    make_super_admin(email="rec-admin3@example.com", password="root-pass-1234")
    website = unsupported_website(
        {
            "wordpress_rest": {"confidence": 0.2, "needs_review": True, "browser_required": False},
            "json_ld_event": {"confidence": 0.15, "needs_review": True, "browser_required": False},
        }
    )
    login("rec-admin3@example.com", "root-pass-1234")

    body = client.get(f"/admin/websites/{website.id}").text
    assert "No detector evidence was recorded for this run." not in body
    assert "wordpress_rest" in body
    assert "json_ld_event" in body


def test_timezone_dst_warning_is_shown(client, make_super_admin, unsupported_website, login):
    make_super_admin(email="rec-tz@example.com", password="root-pass-1234")
    website = unsupported_website({}, timezone_override="EST")
    login("rec-tz@example.com", "root-pass-1234")

    body = client.get(f"/admin/websites/{website.id}").text
    assert "DST warning" in body
    assert "daylight saving" in body.lower()


# --- restricted-browser retry action visibility -----------------------------


def test_retry_button_hidden_when_browser_disabled(
    client, make_super_admin, unsupported_website, login, disable_browser
):
    make_super_admin(email="rec-off@example.com", password="root-pass-1234")
    website = unsupported_website({})
    login("rec-off@example.com", "root-pass-1234")

    body = client.get(f"/admin/websites/{website.id}").text
    assert "Retry with restricted browser detection" not in body
    assert "disabled for this deployment" in body


def test_retry_button_shown_when_browser_enabled(
    client, make_super_admin, unsupported_website, login, enable_browser
):
    make_super_admin(email="rec-on@example.com", password="root-pass-1234")
    website = unsupported_website({})
    login("rec-on@example.com", "root-pass-1234")

    body = client.get(f"/admin/websites/{website.id}").text
    assert "Retry with restricted browser detection" in body


# --- route: permission, CSRF, disabled guard --------------------------------


def test_browser_retry_route_requires_permission(
    client, make_user, make_city, make_website, login, enable_browser
):
    make_user(email="rec-plain@example.com", password="plain-pass-1234", role_name=REGISTERED_USER)
    city = make_city()
    website = make_website(city)
    login("rec-plain@example.com", "plain-pass-1234")

    resp = client.post(
        f"/admin/websites/{website.id}/browser-retry",
        data={"csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert resp.status_code == 403


def test_browser_retry_route_requires_csrf(
    client, make_super_admin, unsupported_website, login, enable_browser
):
    make_super_admin(email="rec-csrf@example.com", password="root-pass-1234")
    website = unsupported_website({})
    login("rec-csrf@example.com", "root-pass-1234")

    resp = client.post(
        f"/admin/websites/{website.id}/browser-retry",
        data={"csrf_token": "wrong-token"},
        follow_redirects=False,
    )
    assert resp.status_code == 403


def test_browser_retry_route_refuses_when_disabled(
    client, make_super_admin, unsupported_website, login, disable_browser
):
    make_super_admin(email="rec-dis@example.com", password="root-pass-1234")
    website = unsupported_website({})
    login("rec-dis@example.com", "root-pass-1234")

    resp = client.post(
        f"/admin/websites/{website.id}/browser-retry",
        data={"csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert resp.status_code == 409


def test_unsupported_review_pages_return_200(
    client, make_super_admin, unsupported_website, login
):
    make_super_admin(email="rec-200@example.com", password="root-pass-1234")
    website = unsupported_website({"wordpress_rest": {"confidence": 0.1}})
    login("rec-200@example.com", "root-pass-1234")

    assert client.get(f"/admin/websites/{website.id}").status_code == 200
    assert client.get(f"/admin/websites/{website.id}/onboarding").status_code == 200


def _recovery_evidence() -> dict:
    return {
        "attempted_at": "2026-07-30T00:00:00+00:00",
        "status": "needs_review",
        "rendered_status": 200,
        "observed_response_types": ["rendered_html", "json"],
        "chosen_source": "rendered_html",
        "selected_endpoint": None,
        "selection_reason": "rendered HTML detected as 'generic_html_cards'",
        "new_pattern_needed": True,
        "ignored_endpoint_count": 3,
        "candidate_event_endpoints": [
            {
                "url": "https://www.visitbloomington.com/includes/rest_v2/"
                "plugins_events_events_by_date/find/",
                "event_likeness_score": 0.91,
                "top_level_type": "object",
                "record_array_path": "docs.docs",
                "sample_field_names": ["id", "startDate", "title", "url"],
                "sample_record_count": 2,
            }
        ],
        "ignored_endpoints": [
            {"url": "https://pixel.spotify.com/v1/track", "origin": "spotify.com",
             "classification": "third_party_telemetry"},
        ],
        "rejected_candidates": [
            {"url": "https://www.visitbloomington.com/includes/rest_v2/"
             "plugins_events_events_by_date/find/",
             "score": 0.91, "reason": "no registered pattern can extract this response shape"},
        ],
        "preview_status": "failed",
        "proposed_pattern": None,
        "detected_pattern": None,
        "detection_confidence": None,
        "discovered_endpoints": [
            "https://www.visitbloomington.com/includes/rest_v2/plugins_events_events_by_date/find/",
            "https://pixel.spotify.com/v1/track",
        ],
        "response_shape": {
            "top_level_type": "object", "top_level_keys": ["docs"],
            "record_array_path": "docs.docs",
            "sample_field_names": ["id", "startDate", "title", "url"],
            "sample_record_count": 2, "event_likeness_score": 0.91,
        },
        "blocked_reason": None,
        "error_summary": "rendered HTML was selected despite a higher-scoring structured candidate",
    }


@pytest.fixture
def report_with_recovery(db_session, make_city, make_website):
    from app.models.unsupported_site_report import UnsupportedSiteReport

    def _make():
        city = make_city()
        website = make_website(city)
        website.onboarding_status = "needs_review"
        db_session.add(website)
        db_session.commit()
        report = UnsupportedSiteReport(
            website_id=website.id,
            submitted_url="https://www.visitbloomington.com/events/events-this-weekend/",
            fingerprint="fp-ui",
            failure_reason="no_pattern_matched",
            browser_recovery=_recovery_evidence(),
        )
        db_session.add(report)
        db_session.commit()
        db_session.refresh(report)
        return report

    return _make


def test_report_detail_separates_candidates_from_ignored(
    client, make_super_admin, report_with_recovery, login
):
    make_super_admin(email="rec-report@example.com", password="root-pass-1234")
    report = report_with_recovery()
    login("rec-report@example.com", "root-pass-1234")

    resp = client.get(f"/admin/unsupported-reports/{report.id}")
    assert resp.status_code == 200
    body = resp.text
    # Candidate event endpoint shown with its analysis.
    assert "Candidate event endpoints" in body
    assert "plugins_events_events_by_date/find/" in body
    assert "docs.docs" in body
    # Selection reason and the "needs a reusable pattern" warning surfaced.
    assert "reusable pattern is needed" in body
    assert "rendered HTML detected as" in body
    # Ignored third-party count shown; telemetry not offered as a candidate.
    assert "Ignored third-party endpoints" in body
    # Rejected candidates and their reasons displayed.
    assert "no registered pattern can extract this response shape" in body


def test_website_detail_shows_recovery_candidates(
    client, make_super_admin, make_city, make_website, login, db_session
):
    from app.models.unsupported_site_report import UnsupportedSiteReport

    make_super_admin(email="rec-wd@example.com", password="root-pass-1234")
    city = make_city()
    website = make_website(city)
    website.onboarding_status = "needs_review"
    website.proposed_pattern = {"detection": _detection({}), "configuration": None}
    db_session.add(website)
    db_session.commit()
    db_session.add(
        UnsupportedSiteReport(
            website_id=website.id, submitted_url="https://example.com/events",
            fingerprint="fp-wd", browser_recovery=_recovery_evidence(),
        )
    )
    db_session.commit()
    login("rec-wd@example.com", "root-pass-1234")

    body = client.get(f"/admin/websites/{website.id}").text
    assert "Candidate event endpoints" in body
    assert "needs a reusable pattern" in body
    assert "plugins_events_events_by_date/find/" in body


def test_browser_retry_route_runs_and_audits(
    client, make_super_admin, unsupported_website, login, enable_browser, db_session
):
    make_super_admin(email="rec-run@example.com", password="root-pass-1234")
    website = unsupported_website({})
    login("rec-run@example.com", "root-pass-1234")

    fake = BrowserRecoveryResult(status="needs_review", observation=None)
    with patch(
        "app.routers.websites.browser_retry_recovery", new=AsyncMock(return_value=fake)
    ) as mocked:
        resp = client.post(
            f"/admin/websites/{website.id}/browser-retry",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    mocked.assert_awaited_once()
    audit = (
        db_session.query(AuditLog)
        .filter(AuditLog.action == "website_browser_retry")
        .one()
    )
    assert audit.entity_id == website.id
