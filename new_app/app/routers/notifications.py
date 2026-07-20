from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.core.csrf import verify_csrf
from app.core.exceptions import AppError, NotFoundError
from app.core.flash import set_flash
from app.core.templating import render
from app.dependencies import ClientIp, CorrelationId, CurrentUser, DbSession
from app.repositories.notification import (
    dismiss as dismiss_notification,
)
from app.repositories.notification import (
    get_notification,
    list_notifications_for_user,
)
from app.repositories.notification import (
    mark_read as mark_notification_read,
)
from app.services.audit import record_audit
from app.services.rbac import can_access_admin

router = APIRouter(prefix="/admin/notifications", tags=["admin-notifications"])


def _own_notification_or_404(db, notification_id: int, user_id: int):
    notification = get_notification(db, notification_id)
    if notification is None or notification.recipient_user_id != user_id:
        raise NotFoundError("Notification not found")
    return notification


@router.get("", response_class=HTMLResponse)
def list_notifications_view(
    request: Request, current_user: CurrentUser, db: DbSession, unread_only: bool = False
):
    if not can_access_admin(db, current_user):
        raise AppError("Forbidden: no admin access", status_code=403)

    notifications = list_notifications_for_user(
        db, current_user.id, unread_only=unread_only, limit=50
    )
    return render(
        request,
        "admin/notifications/list.html",
        {
            "current_user": current_user,
            "notifications": notifications,
            "unread_only": unread_only,
        },
    )


@router.post("/{notification_id}/read")
def mark_read_view(
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
    response = RedirectResponse(url="/admin/notifications", status_code=303)
    return response


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
