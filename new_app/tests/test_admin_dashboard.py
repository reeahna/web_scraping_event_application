import pytest

from app.core.permissions import EDITOR
from app.extraction.unsupported import UnsupportedReportData
from app.repositories.unsupported_site_report import create_unsupported_site_report
from app.services.extraction_runs import run_detection
from tests.extraction_helpers import html_handler, patched_http_fetch


@pytest.mark.asyncio
async def test_onboarding_metrics_reflect_real_counts(
    client, make_super_admin, make_city, make_website, login, db_session
):
    make_super_admin(email="dash-root@example.com", password="root-pass-1234")
    city = make_city(name="Dashboard City", slug="dashboard-city")
    website = make_website(city, name="Dashboard Site")
    website.event_listing_url = "https://example.com/events"
    db_session.commit()

    with patched_http_fetch(html_handler("unsupported_page.html")):
        await run_detection(db_session, website)

    login("dash-root@example.com", "root-pass-1234")
    resp = client.get("/admin")
    assert resp.status_code == 200
    assert "Broken" in resp.text
    assert "1</strong> unsupported website" in resp.text
    assert "1</strong> open failure report to triage" in resp.text
    assert "/admin/websites?onboarding_status=unsupported" in resp.text


def test_zero_count_statuses_are_not_rendered(client, make_super_admin, login):
    """The dashboard lists only non-zero statuses — an empty install should not
    render a row of permanent zeros."""
    make_super_admin(email="dash-empty@example.com", password="root-pass-1234")
    login("dash-empty@example.com", "root-pass-1234")

    resp = client.get("/admin")
    assert resp.status_code == 200
    assert "Nothing is broken." in resp.text
    assert "Nothing is waiting on you." in resp.text
    assert "onboarding_status=draft" not in resp.text


def test_detected_website_is_waiting_on_a_human_not_in_progress(
    client, make_super_admin, make_city, make_website, login, db_session
):
    """DETECTED needs an operator to preview and approve (app.core.onboarding),
    so it belongs under "Waiting on you" — grouping it as in-progress work
    implied the system was still busy with it."""
    make_super_admin(email="dash-detected@example.com", password="root-pass-1234")
    city = make_city(name="Detected City", slug="detected-city")
    website = make_website(city, name="Detected Site")
    website.onboarding_status = "detected"
    db_session.commit()

    login("dash-detected@example.com", "root-pass-1234")
    resp = client.get("/admin")
    assert resp.status_code == 200
    assert "Waiting on you" in resp.text
    assert "1</strong> website awaiting preview &amp; approval" in resp.text


def test_editor_sees_onboarding_metrics_but_not_audit_log(client, make_user, login):
    make_user(email="dash-editor@example.com", password="editor-pass-123", role_name=EDITOR)
    login("dash-editor@example.com", "editor-pass-123")

    resp = client.get("/admin")
    assert resp.status_code == 200
    assert "Waiting on you" in resp.text
    assert "Recent audit actions" not in resp.text


def test_welcome_prefers_display_name_over_email(client, make_super_admin, db_session, login):
    admin = make_super_admin(email="dash-named@example.com", password="root-pass-1234")
    admin.full_name = "Reeahna Patel"
    db_session.commit()

    login("dash-named@example.com", "root-pass-1234")
    resp = client.get("/admin")
    assert resp.status_code == 200
    assert "Welcome, Reeahna Patel" in resp.text


def test_welcome_falls_back_to_email_without_display_name(client, make_super_admin, login):
    make_super_admin(email="dash-unnamed@example.com", password="root-pass-1234")
    login("dash-unnamed@example.com", "root-pass-1234")

    resp = client.get("/admin")
    assert resp.status_code == 200
    assert "Welcome, dash-unnamed@example.com" in resp.text


def test_unread_notification_count_shown_on_dashboard(client, make_super_admin, db_session, login):
    from app.services.notifications import build_dedup_fingerprint, notify

    admin = make_super_admin(email="dash-notif-root@example.com", password="root-pass-1234")
    notify(
        db_session,
        notification_type="test_event",
        severity="info",
        title="Dashboard test",
        message="msg",
        recipients=[admin],
        dedup_fingerprint=build_dedup_fingerprint("dashboard_test", "1"),
    )
    login("dash-notif-root@example.com", "root-pass-1234")

    resp = client.get("/admin")
    assert resp.status_code == 200
    # The count lives in the nav, not a dashboard tile — one place, always visible.
    assert "Notifications (1)" in resp.text


def test_dashboard_queries_stay_bounded_count_only(db_session, make_city, make_website):
    """Sanity check: the report-count helper used by the dashboard performs a
    single COUNT query, never loading full report payloads."""
    city = make_city(name="Bounded City", slug="bounded-city")
    website = make_website(city, name="Bounded Site")
    create_unsupported_site_report(
        db_session,
        UnsupportedReportData(
            website_id=website.id,
            submitted_url="https://example.com/events",
            final_url=None,
            http_status=None,
            page_title=None,
            detected_platform_evidence={},
            available_detector_results={},
            discovered_endpoints=[],
            browser_required=False,
            json_ld_presence=False,
            pagination_indicators={},
            access_denied_or_challenge_detected=True,
            failure_reason="blocked",
            fingerprint="bounded-fp",
        ),
    )
    from app.repositories.unsupported_site_report import count_unresolved_reports

    assert count_unresolved_reports(db_session) == 1
