"""Unsupported-site-report lifecycle operations: assignment, notes, and
validated status transitions. Thin service layer over
app.repositories.unsupported_site_report, mirroring
app.services.website_configuration's style — permission checks and auditing
stay in the router, this module only enforces the report's own state machine
(app.core.report_status)."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.exceptions import AppError
from app.core.report_status import DISMISSED, OPEN, RESOLVED, can_transition_report
from app.models.unsupported_site_report import UnsupportedSiteReport
from app.models.user import User
from app.repositories.unsupported_site_report import (
    add_report_note,
    assign_report,
    change_report_status,
)
from app.services.notifications import SEVERITY_INFO, build_dedup_fingerprint, notify


def assign(
    db: Session,
    report: UnsupportedSiteReport,
    *,
    assigned_user_id: int | None,
    correlation_id: str | None = None,
) -> UnsupportedSiteReport:
    report = assign_report(db, report, assigned_user_id=assigned_user_id)
    if assigned_user_id is not None:
        recipient = db.get(User, assigned_user_id)
        notify(
            db,
            notification_type="unsupported_report_assigned",
            severity=SEVERITY_INFO,
            title="Unsupported-site report assigned to you",
            message=f"Report #{report.id} for '{report.website.name}' was assigned to you.",
            recipients=[recipient] if recipient else [],
            related_resource_type="unsupported_site_report",
            related_resource_id=report.id,
            action_url=f"/admin/unsupported-reports/{report.id}",
            dedup_fingerprint=build_dedup_fingerprint(
                "unsupported_report_assigned", str(report.id), str(assigned_user_id)
            ),
            correlation_id=correlation_id,
        )
    return report


def add_note(db: Session, report: UnsupportedSiteReport, *, note: str) -> UnsupportedSiteReport:
    note = note.strip()
    if not note:
        raise AppError("Note cannot be empty.", status_code=400)
    if len(note) > 2000:
        raise AppError("Note must be 2000 characters or fewer.", status_code=400)
    return add_report_note(db, report, note=note)


def change_status(
    db: Session, report: UnsupportedSiteReport, *, target_status: str, changed_by_user_id: int
) -> UnsupportedSiteReport:
    if not can_transition_report(report.status, target_status):
        raise AppError(
            f"Cannot move a report from '{report.status}' to '{target_status}'.",
            status_code=409,
        )
    return change_report_status(
        db, report, status=target_status, resolved_by_user_id=changed_by_user_id
    )


def resolve(
    db: Session,
    report: UnsupportedSiteReport,
    *,
    resolved_by_user_id: int,
    correlation_id: str | None = None,
) -> UnsupportedSiteReport:
    previously_assigned_user_id = report.assigned_user_id
    report = change_status(
        db, report, target_status=RESOLVED, changed_by_user_id=resolved_by_user_id
    )
    if previously_assigned_user_id is not None:
        recipient = db.get(User, previously_assigned_user_id)
        notify(
            db,
            notification_type="unsupported_report_resolved",
            severity=SEVERITY_INFO,
            title="Unsupported-site report resolved",
            message=f"Report #{report.id} for '{report.website.name}' was resolved.",
            recipients=[recipient] if recipient else [],
            related_resource_type="unsupported_site_report",
            related_resource_id=report.id,
            action_url=f"/admin/unsupported-reports/{report.id}",
            dedup_fingerprint=build_dedup_fingerprint(
                "unsupported_report_resolved", str(report.id)
            ),
            correlation_id=correlation_id,
        )
    return report


def dismiss(
    db: Session, report: UnsupportedSiteReport, *, dismissed_by_user_id: int
) -> UnsupportedSiteReport:
    return change_status(
        db, report, target_status=DISMISSED, changed_by_user_id=dismissed_by_user_id
    )


def reopen(
    db: Session, report: UnsupportedSiteReport, *, reopened_by_user_id: int
) -> UnsupportedSiteReport:
    return change_status(db, report, target_status=OPEN, changed_by_user_id=reopened_by_user_id)
