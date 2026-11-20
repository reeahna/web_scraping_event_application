from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.core.csrf import verify_csrf
from app.core.exceptions import AppError, NotFoundError
from app.core.flash import set_flash
from app.core.templating import render
from app.dependencies import ClientIp, CorrelationId, CurrentUser, DbSession
from app.repositories.notification import (
    READ_STATE_READ,
    READ_STATE_UNREAD,
    count_unread_for_user,
    get_notification,
    mark_all_read_for_user,
    page_notifications_for_user,
)
from app.repositories.notification import (
    dismiss as dismiss_notification,
)
from app.repositories.notification import (
    mark_read as mark_notification_read,
)
from app.services.audit import record_audit
from app.services.rbac import can_access_admin

router = APIRouter(prefix="/admin/notifications", tags=["admin-notifications"])

PER_PAGE = 50
LIST_URL = "/admin/notifications"

# Filter values -> labels, in the order the filter menus show them.
READ_STATES = {READ_STATE_UNREAD: "Unread", READ_STATE_READ: "Read"}
SEVERITIES = {"error": "Errors", "warning": "Warnings", "info": "Info"}


def _clean_filters(
    status: str | None, severity: str | None, unread_only: bool = False
) -> tuple[str, str]:
    """Known filter values only; anything else means "all". `unread_only` is
    the older checkbox parameter and still means status=unread."""
    status = (status or "").strip().lower()
    severity = (severity or "").strip().lower()
    if status not in READ_STATES:
        status = READ_STATE_UNREAD if unread_only else ""
    if severity not in SEVERITIES:
        severity = ""
    return status, severity


def _list_url(status: str, severity: str, page: int = 1) -> str:
    """The list URL for a filter combination and page. Built from cleaned values
    only, never from a submitted URL, so redirects stay on this page."""
    params = [("status", status), ("severity", severity)]
    if page > 1:
        params.append(("page", str(page)))
    query = urlencode([(k, v) for k, v in params if v])
    return f"{LIST_URL}?{query}" if query else LIST_URL


def _own_notification_or_404(db, notification_id: int, user_id: int):
    notification = get_notification(db, notification_id)
    if notification is None or notification.recipient_user_id != user_id:
        raise NotFoundError("Notification not found")
    return notification


@router.get("", response_class=HTMLResponse)
def list_notifications_view(
    request: Request,
    current_user: CurrentUser,
    db: DbSession,
    status: str | None = None,
    severity: str | None = None,
    unread_only: bool = False,
    page: int = 1,
):
    if not can_access_admin(db, current_user):
        raise AppError("Forbidden: no admin access", status_code=403)

    status, severity = _clean_filters(status, severity, unread_only)
    notifications, total, page = page_notifications_for_user(
        db,
        current_user.id,
        read_state=status or None,
        severity=severity or None,
        page=page,
        per_page=PER_PAGE,
    )
    filter_query = urlencode([(k, v) for k, v in (("status", status), ("severity", severity)) if v])
    return render(
        request,
        "admin/notifications/list.html",
        {
            "current_user": current_user,
            "notifications": notifications,
            "status": status,
            "severity": severity,
            "read_states": READ_STATES,
            "severities": SEVERITIES,
            "filtered": bool(status or severity),
            "unread_only": status == READ_STATE_UNREAD,
            "total": total,
            "page": page,
            "per_page": PER_PAGE,
            "total_pages": max(1, -(-total // PER_PAGE)),
            # Ends in "?" or "&" so the pager can append page=N and keep the filters.
            "base_url": f"{LIST_URL}?{filter_query}&" if filter_query else f"{LIST_URL}?",
            "unread_total": count_unread_for_user(db, current_user.id),
        },
    )


@router.post("/read-all")
def mark_all_read_view(
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    ip_address: ClientIp,
    current_user: CurrentUser,
    csrf_token: str = Form(...),
    unread_only: bool = Form(False),
    status: str = Form(""),
    severity: str = Form(""),
):
    verify_csrf(request, csrf_token)
    if not can_access_admin(db, current_user):
        raise AppError("Forbidden: no admin access", status_code=403)
    changed = mark_all_read_for_user(db, current_user.id)
    if changed:
        record_audit(
            db,
            actor_id=current_user.id,
            action="notifications_marked_all_read",
            entity_type="notification",
            after={"count": changed},
            correlation_id=correlation_id,
            ip_address=ip_address,
        )
    if unread_only and not status:
        url = "/admin/notifications?unread_only=true"
    else:
        url = _list_url(*_clean_filters(status, severity))
    response = RedirectResponse(url=url, status_code=303)
    if changed:
        noun = "notification" if changed == 1 else "notifications"
        set_flash(response, f"Marked {changed} {noun} as read.")
    else:
        set_flash(response, "No unread notifications.")
    return response


@router.post("/{notification_id}/read")
def mark_read_view(
    notification_id: int,
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    ip_address: ClientIp,
    current_user: CurrentUser,
    csrf_token: str = Form(...),
    status: str = Form(""),
    severity: str = Form(""),
    page: int = Form(1),
):
    verify_csrf(request, csrf_token)
    if not can_access_admin(db, current_user):
        raise AppError("Forbidden: no admin access", status_code=403)
    notification = _own_notification_or_404(db, notification_id, current_user.id)
    mark_notification_read(db, notification)
    record_audit(
        db,
        actor_id=current_user.id,
        action="notification_marked_read",
        entity_type="notification",
        entity_id=notification.id,
        correlation_id=correlation_id,
        ip_address=ip_address,
    )
    # Back to the same filters and page. If that page no longer exists (the last
    # unread row on it was just read), the list shows the nearest page instead.
    url = _list_url(*_clean_filters(status, severity), page=max(1, page))
    return RedirectResponse(url=url, status_code=303)


@router.post("/{notification_id}/dismiss")
def dismiss_view(
    notification_id: int,
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    ip_address: ClientIp,
    current_user: CurrentUser,
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    if not can_access_admin(db, current_user):
        raise AppError("Forbidden: no admin access", status_code=403)
    notification = _own_notification_or_404(db, notification_id, current_user.id)
    dismiss_notification(db, notification)
    record_audit(
        db,
        actor_id=current_user.id,
        action="notification_dismissed",
        entity_type="notification",
        entity_id=notification.id,
        correlation_id=correlation_id,
        ip_address=ip_address,
    )
    response = RedirectResponse(url="/admin/notifications", status_code=303)
    set_flash(response, "Notification dismissed.")
    return response
