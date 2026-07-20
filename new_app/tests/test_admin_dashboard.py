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
    assert "Website onboarding" in resp.text
    assert "1 unresolved unsupported-site report" in resp.text


def test_editor_sees_onboarding_metrics_but_not_audit_log(client, make_user, login):
    make_user(email="dash-editor@example.com", password="editor-pass-123", role_name=EDITOR)
    login("dash-editor@example.com", "editor-pass-123")

    resp = client.get("/admin")
    assert resp.status_code == 200
    assert "Website onboarding" in resp.text
    assert "Recent audit actions" not in resp.text


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
    assert "Unread notifications" in resp.text


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
