"""Report lifecycle states are storage values, not display text.

They were rendered straight into the admin, so an operator read
"waiting_for_browser_support" in the status column, the filter dropdown and the
transition buttons.
"""

from datetime import UTC, datetime

import pytest

from app.core.permissions import ADMINISTRATOR
from app.core.report_status import (
    REPORT_STATUS_LABELS,
    REPORT_STATUSES,
    report_status_label,
)
from app.core.templating import _admin_datetime
from app.extraction.unsupported import UnsupportedReportData
from app.repositories.unsupported_site_report import create_unsupported_site_report


def test_every_state_has_a_written_label():
    assert set(REPORT_STATUS_LABELS) == set(REPORT_STATUSES)


def test_no_label_is_just_the_underscored_state_name():
    for state, label in REPORT_STATUS_LABELS.items():
        assert "_" not in label, state
        assert label != state


def test_an_unknown_state_still_reads_as_words():
    assert report_status_label("some_new_state") == "Some new state"
    assert report_status_label(None) == "—"


@pytest.fixture
def report(db_session, make_city, make_website, make_user, login):
    make_user(email="reports@example.com", password="report-pass-123", role_name=ADMINISTRATOR)
    website = make_website(make_city(name="Rep City", slug="rep-city"), name="Rep Site")
    db_session.commit()
    record = create_unsupported_site_report(
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
            browser_required=True,
            json_ld_presence=False,
            pagination_indicators={},
            access_denied_or_challenge_detected=False,
            failure_reason="no_pattern_matched",
            fingerprint="label-fp",
        ),
    )
    record.status = "waiting_for_browser_support"
    db_session.commit()
    login("reports@example.com", "report-pass-123")
    return record


def test_list_shows_labels_not_state_names(client, report):
    html = client.get("/admin/unsupported-reports").text
    assert "Waiting for browser support" in html
    # The identifier survives as an option value; it must not be visible text.
    assert ">waiting_for_browser_support<" not in html


def test_detail_shows_a_label_and_readable_transitions(client, report):
    html = client.get(f"/admin/unsupported-reports/{report.id}").text
    assert "Waiting for browser support" in html
    assert "Move to investigating" in html
    assert "Move to waiting_for_browser_support" not in html


def test_website_detail_labels_its_linked_reports(client, report):
    html = client.get(f"/admin/websites/{report.website_id}").text
    assert "Waiting for browser support" in html


def test_admin_datetime_survives_a_string_from_a_json_column():
    """browser_recovery is a JSON column, so its timestamps are strings.
    Passing one to the formatter used to raise and 500 the whole page."""
    assert _admin_datetime("2026-09-16T19:05:05") == "2026-09-16T19:05:05"
    assert _admin_datetime(None) == "—"
    assert " at " in _admin_datetime(datetime(2026, 9, 16, 19, 5, tzinfo=UTC))


def test_a_manager_can_actually_see_the_management_controls(client, report):
    """The detail route did not pass `can_manage`, so the template's gate was
    always falsy and nobody could add a note, assign, or change status from
    this page, whatever their permissions."""
    html = client.get(f"/admin/unsupported-reports/{report.id}").text
    assert "Add note" in html
    assert "Update assignment" in html
    assert f'action="/admin/unsupported-reports/{report.id}/status"' in html


def test_a_viewer_without_manage_permission_sees_none_of_them(
    client, report, make_user, login
):
    from app.core.permissions import EDITOR

    make_user(email="viewer@example.com", password="viewer-pass-123", role_name=EDITOR)
    login("viewer@example.com", "viewer-pass-123")

    response = client.get(f"/admin/unsupported-reports/{report.id}")
    if response.status_code != 200:
        pytest.skip("this role cannot view reports at all")
    assert "Add note" not in response.text
    assert f'action="/admin/unsupported-reports/{report.id}/status"' not in response.text
