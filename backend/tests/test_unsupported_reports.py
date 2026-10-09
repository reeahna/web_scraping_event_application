import pytest

from app.core.exceptions import AppError
from app.core.permissions import ADMINISTRATOR, EDITOR, REGISTERED_USER
from app.extraction.unsupported import UnsupportedReportData
from app.models.audit_log import AuditLog
from app.repositories.unsupported_site_report import create_unsupported_site_report, list_reports
from app.services.extraction_runs import run_detection
from app.services.unsupported_reports import add_note, assign, dismiss, reopen, resolve
from tests.extraction_helpers import html_handler, patched_http_fetch


def _csrf(client) -> str:
    return client.cookies.get("csrf_token")


def _report_data(website_id: int, fingerprint: str) -> UnsupportedReportData:
    return UnsupportedReportData(
        website_id=website_id,
        submitted_url="https://example.com/events",
        final_url="https://example.com/events",
        http_status=200,
        page_title="Some Site",
        detected_platform_evidence={},
        available_detector_results={},
        discovered_endpoints=[],
        browser_required=False,
        json_ld_presence=False,
        pagination_indicators={},
        access_denied_or_challenge_detected=False,
        failure_reason="no_pattern_matched",
        fingerprint=fingerprint,
    )


@pytest.fixture
def report(db_session, make_city, make_website):
    city = make_city(name="Report City", slug="report-city")
    website = make_website(city, name="Report Site")
    return create_unsupported_site_report(db_session, _report_data(website.id, "fp-1"))


# --- Occurrence tracking (via run_detection) ------------------------------------------


@pytest.mark.asyncio
async def test_repeated_unchanged_detection_bumps_occurrence_not_new_report(
    db_session, make_city, make_website
):
    city = make_city(name="Occurrence City", slug="occurrence-city")
    website = make_website(city, name="Occurrence Site")
    website.event_listing_url = "https://example.com/events"
    db_session.commit()

    with patched_http_fetch(html_handler("unsupported_page.html")):
        await run_detection(db_session, website)
        await run_detection(db_session, website)
        await run_detection(db_session, website)

    reports, total = list_reports(db_session, website_id=website.id)
    assert total == 1
    assert reports[0].occurrence_count == 3


# --- Lifecycle service functions -------------------------------------------------------


def test_assign_updates_assigned_user(db_session, report, make_user):
    assignee = make_user(email="assignee@example.com")
    updated = assign(db_session, report, assigned_user_id=assignee.id)
    assert updated.assigned_user_id == assignee.id


def test_add_note_appends_to_existing_notes(db_session, report):
    add_note(db_session, report, note="first note")
    add_note(db_session, report, note="second note")
    assert "first note" in report.admin_notes
    assert "second note" in report.admin_notes


def test_add_note_rejects_empty_note(db_session, report):
    with pytest.raises(AppError):
        add_note(db_session, report, note="   ")


def test_resolve_dismiss_reopen_lifecycle(db_session, report, make_user):
    admin = make_user(email="lifecycle-admin@example.com")

    resolve(db_session, report, resolved_by_user_id=admin.id)
    assert report.status == "resolved"
    assert report.resolved_at is not None
    assert report.resolved_by_user_id == admin.id

    reopen(db_session, report, reopened_by_user_id=admin.id)
    assert report.status == "open"
    assert report.resolved_at is None

    dismiss(db_session, report, dismissed_by_user_id=admin.id)
    assert report.status == "dismissed"
    assert report.resolved_at is not None


def test_illegal_status_transition_rejected(db_session, report, make_user):
    from app.services.unsupported_reports import change_status

    admin = make_user(email="illegal-admin@example.com")
    dismiss(db_session, report, dismissed_by_user_id=admin.id)

    with pytest.raises(AppError):
        change_status(db_session, report, target_status="resolved", changed_by_user_id=admin.id)


# --- Permissions and routes -------------------------------------------------------------


def test_reports_view_permission_gates_list_and_detail(client, make_user, report, login):
    make_user(email="viewer-editor@example.com", password="editor-pass-123", role_name=EDITOR)
    login("viewer-editor@example.com", "editor-pass-123")

    assert client.get("/admin/unsupported-reports").status_code == 200
    assert client.get(f"/admin/unsupported-reports/{report.id}").status_code == 200


def test_registered_user_denied_reports_list(client, make_user, login):
    make_user(
        email="reports-denied@example.com", password="denied-pass-123", role_name=REGISTERED_USER
    )
    login("reports-denied@example.com", "denied-pass-123")

    assert client.get("/admin/unsupported-reports").status_code == 403


def test_editor_cannot_assign_or_change_status(client, make_user, report, login):
    make_user(email="manage-editor@example.com", password="editor-pass-123", role_name=EDITOR)
    login("manage-editor@example.com", "editor-pass-123")

    resp = client.post(
        f"/admin/unsupported-reports/{report.id}/assign",
        data={"assigned_user_id": "", "csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert resp.status_code == 403


def test_administrator_can_assign_and_change_status(client, make_user, report, login, db_session):
    admin = make_user(
        email="manage-admin@example.com", password="admin-pass-123", role_name=ADMINISTRATOR
    )
    login("manage-admin@example.com", "admin-pass-123")

    resp = client.post(
        f"/admin/unsupported-reports/{report.id}/assign",
        data={"assigned_user_id": str(admin.id), "csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db_session.refresh(report)
    assert report.assigned_user_id == admin.id

    resp = client.post(
        f"/admin/unsupported-reports/{report.id}/status",
        data={"target_status": "resolved", "csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db_session.refresh(report)
    assert report.status == "resolved"

    entries = (
        db_session.query(AuditLog)
        .filter(AuditLog.action.in_(("unsupported_report_assigned", "unsupported_report_resolved")))
        .all()
    )
    assert {e.action for e in entries} == {
        "unsupported_report_assigned",
        "unsupported_report_resolved",
    }


def test_list_reports_filters_by_status_and_browser_required(db_session, make_city, make_website):
    city = make_city(name="Filter City", slug="filter-city")
    website = make_website(city, name="Filter Site")
    create_unsupported_site_report(db_session, _report_data(website.id, "fp-open"))
    browser_report_data = _report_data(website.id, "fp-browser")
    browser_report_data = UnsupportedReportData(
        **{**browser_report_data.__dict__, "browser_required": True}
    )
    create_unsupported_site_report(db_session, browser_report_data)

    reports, total = list_reports(db_session, browser_required=True)
    assert total == 1
    assert reports[0].browser_required is True
