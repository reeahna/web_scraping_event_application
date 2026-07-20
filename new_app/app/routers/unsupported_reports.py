from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.core.csrf import verify_csrf
from app.core.exceptions import AppError, NotFoundError
from app.core.flash import set_flash
from app.core.report_status import ALLOWED_REPORT_TRANSITIONS, REPORT_STATUSES
from app.core.templating import render
from app.dependencies import ClientIp, CorrelationId, DbSession
from app.models.user import User
from app.repositories.city import list_cities
from app.repositories.unsupported_site_report import get_report, list_reports
from app.services.audit import record_audit
from app.services.rbac import require_permission, user_has_permission, users_with_permission
from app.services.unsupported_reports import add_note, assign, dismiss, reopen, resolve

router = APIRouter(prefix="/admin/unsupported-reports", tags=["admin-unsupported-reports"])

ViewReports = Annotated[User, Depends(require_permission("reports.view"))]
ManageReports = Annotated[User, Depends(require_permission("reports.manage"))]

PER_PAGE = 20


@router.get("", response_class=HTMLResponse)
def list_reports_view(
    request: Request,
    current_user: ViewReports,
    db: DbSession,
    status: str | None = None,
    city_id: int | None = None,
    website_id: int | None = None,
    browser_required: bool | None = None,
    page: int = 1,
):
    reports, total = list_reports(
        db,
        status=status,
        city_id=city_id,
        website_id=website_id,
        browser_required=browser_required,
        page=page,
        per_page=PER_PAGE,
    )
    cities = list_cities(db, active_only=False)
    base_url = (
        f"/admin/unsupported-reports?status={status or ''}&city_id={city_id or ''}"
        f"&website_id={website_id or ''}"
    )
    return render(
        request,
        "admin/unsupported_reports/list.html",
        {
            "current_user": current_user,
            "reports": reports,
            "total": total,
            "page": page,
            "per_page": PER_PAGE,
            "status": status or "",
            "city_id": city_id,
            "website_id": website_id,
            "browser_required": browser_required,
            "cities": cities,
            "all_statuses": REPORT_STATUSES,
            "base_url": base_url,
            "can_manage": user_has_permission(db, current_user, "reports.manage"),
        },
    )


@router.get("/{report_id}", response_class=HTMLResponse)
def report_detail(report_id: int, request: Request, current_user: ViewReports, db: DbSession):
    report = get_report(db, report_id)
    if report is None:
        raise NotFoundError("Unsupported-site report not found")

    return render(
        request,
        "admin/unsupported_reports/detail.html",
        {
            "current_user": current_user,
            "report": report,
            "next_statuses": sorted(ALLOWED_REPORT_TRANSITIONS.get(report.status, frozenset())),
            "assignable_users": users_with_permission(db, "reports.manage"),
        },
    )


@router.post("/{report_id}/assign")
def assign_report_view(
    report_id: int,
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    ip_address: ClientIp,
    current_user: ManageReports,
    assigned_user_id: str = Form(""),
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    report = get_report(db, report_id)
    if report is None:
        raise NotFoundError("Unsupported-site report not found")

    before = {"assigned_user_id": report.assigned_user_id}
    new_assignee = int(assigned_user_id) if assigned_user_id else None
    assign(db, report, assigned_user_id=new_assignee, correlation_id=correlation_id)
    record_audit(
        db,
        actor_id=current_user.id,
        action="unsupported_report_assigned",
        entity_type="unsupported_site_report",
        entity_id=report.id,
        before=before,
        after={"assigned_user_id": report.assigned_user_id},
        correlation_id=correlation_id,
        ip_address=ip_address,
    )
    response = RedirectResponse(url=f"/admin/unsupported-reports/{report.id}", status_code=303)
    set_flash(response, "Report assignment updated.")
    return response


@router.post("/{report_id}/notes")
def add_report_note_view(
    report_id: int,
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    ip_address: ClientIp,
    current_user: ManageReports,
    note: str = Form(...),
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    report = get_report(db, report_id)
    if report is None:
        raise NotFoundError("Unsupported-site report not found")

    add_note(db, report, note=note)
    record_audit(
        db,
        actor_id=current_user.id,
        action="unsupported_report_note_added",
        entity_type="unsupported_site_report",
        entity_id=report.id,
        detail=note,
        correlation_id=correlation_id,
        ip_address=ip_address,
    )
    response = RedirectResponse(url=f"/admin/unsupported-reports/{report.id}", status_code=303)
    set_flash(response, "Note added.")
    return response


@router.post("/{report_id}/status")
def change_report_status_view(
    report_id: int,
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    ip_address: ClientIp,
    current_user: ManageReports,
    target_status: str = Form(...),
    reason: str = Form(""),
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    report = get_report(db, report_id)
    if report is None:
        raise NotFoundError("Unsupported-site report not found")
    if target_status not in REPORT_STATUSES:
        raise AppError("Invalid target status", status_code=400)

    before = {"status": report.status}
    if target_status == "resolved":
        resolve(db, report, resolved_by_user_id=current_user.id, correlation_id=correlation_id)
        audit_action = "unsupported_report_resolved"
    elif target_status == "dismissed":
        dismiss(db, report, dismissed_by_user_id=current_user.id)
        audit_action = "unsupported_report_status_changed"
    elif target_status == "open" and report.status in ("resolved", "dismissed"):
        reopen(db, report, reopened_by_user_id=current_user.id)
        audit_action = "unsupported_report_status_changed"
    else:
        from app.services.unsupported_reports import change_status

        change_status(db, report, target_status=target_status, changed_by_user_id=current_user.id)
        audit_action = "unsupported_report_status_changed"

    record_audit(
        db,
        actor_id=current_user.id,
        action=audit_action,
        entity_type="unsupported_site_report",
        entity_id=report.id,
        before=before,
        after={"status": report.status},
        detail=reason or None,
        correlation_id=correlation_id,
        ip_address=ip_address,
    )
    response = RedirectResponse(url=f"/admin/unsupported-reports/{report.id}", status_code=303)
    set_flash(response, f"Report status changed to '{report.status}'.")
    return response
