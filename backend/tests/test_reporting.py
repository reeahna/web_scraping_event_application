"""Phase 15: operational reporting service and dashboard."""

from __future__ import annotations

from datetime import UTC, datetime

from app.core.permissions import REGISTERED_USER
from app.models.audit_log import AuditLog
from app.models.extraction_run import ExtractionRun
from app.services.reporting import build_operational_report


def test_empty_report_has_zero_counts_and_no_error(db_session):
    report = build_operational_report(db_session)
    assert report.counts["active_sources"] == 0
    assert report.recent_runs == []
    assert report.recent_audits == []
    assert report.scheduler["running_count"] == 0
    assert report.ai_usage["enabled"] is False


def test_report_reflects_seeded_data(db_session, make_city, make_website):
    city = make_city()
    website = make_website(city, is_active=True,
                           approved_pattern={"pattern_name": "static_html"})
    website.onboarding_status = "active"
    db_session.add(
        ExtractionRun(
            website_id=website.id, run_type="scheduled", status="success",
            source_url="https://x/e", events_found=5, events_valid=5, events_rejected=0,
            warnings=["recurrence_truncated_per_parent", "geographic_filter_excluded:2"],
            started_at=datetime.now(UTC),
        )
    )
    db_session.add(
        AuditLog(action="thing_happened", entity_type="website", entity_id=website.id,
                 actor_type="user", before_state="SECRET-BEFORE", after_state="SECRET-AFTER")
    )
    db_session.commit()

    report = build_operational_report(db_session)
    assert report.counts["active_sources"] == 1
    assert report.counts["recurrence_truncations"] == 1
    assert report.counts["geography_exclusions"] == 1
    assert any(r["status"] == "success" for r in report.recent_runs)
    assert any(a["action"] == "thing_happened" for a in report.recent_audits)


def test_audit_payloads_are_never_surfaced(db_session, make_city):
    db_session.add(
        AuditLog(action="sensitive", actor_type="user",
                 before_state="SECRET-BEFORE", after_state="SECRET-AFTER")
    )
    db_session.commit()
    report = build_operational_report(db_session)
    audit = report.recent_audits[0]
    assert "before_state" not in audit
    assert "after_state" not in audit
    assert "SECRET" not in str(audit)


def test_ai_usage_never_includes_a_key(db_session):
    report = build_operational_report(db_session)
    assert "api_key" not in report.ai_usage
    assert "key" not in report.ai_usage


def test_dashboard_requires_permission(client, make_user, make_super_admin, login):
    # Anonymous.
    assert client.get("/admin/reports").status_code in (401, 403)
    # Registered user without reports.view.
    make_user(email="reg@example.com", password="password12345", role_name=REGISTERED_USER)
    login("reg@example.com", "password12345")
    assert client.get("/admin/reports").status_code == 403


def test_dashboard_renders_for_admin(client, make_super_admin, login):
    make_super_admin(email="root@example.com", password="root-pass-1234")
    login("root@example.com", "root-pass-1234")
    resp = client.get("/admin/reports")
    assert resp.status_code == 200
    assert "Operational dashboard" in resp.text
