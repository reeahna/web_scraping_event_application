from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import get_settings
from app.core.csrf import verify_csrf
from app.core.exceptions import AppError
from app.core.flash import set_flash
from app.core.security import verify_password
from app.core.templating import render
from app.dependencies import ClientIp, CorrelationId, CurrentUser, DbSession
from app.services import engagement
from app.services.account_deletion import delete_account
from app.services.account_email import send_verification_email
from app.services.audit import record_audit
from app.services.email import email_delivery_available
from app.services.rate_limit import get_rate_limit_backend
from app.services.rbac import can_access_admin, get_effective_permissions

router = APIRouter(tags=["account"])

_MAX_DISPLAY_NAME_LENGTH = 255
_ACCOUNT_FORM_FIELDS = frozenset({"display_name", "csrf_token"})


def _render_account(
    request: Request,
    current_user: CurrentUser,
    db: DbSession,
    *,
    display_name: str | None = None,
    errors: dict[str, str] | None = None,
    edit_mode: bool = False,
    status_code: int = 200,
) -> HTMLResponse:
    has_admin_access = can_access_admin(db, current_user)
    role_names = sorted({ur.role.name for ur in current_user.user_roles if ur.role.is_active})
    # Only admins have a meaningful permission set, and the list is long enough
    # (28 for a super admin) that it is reference material, not something to
    # read top to bottom — the template keeps it collapsed.
    permissions = sorted(get_effective_permissions(db, current_user)) if has_admin_access else []
    return render(
        request,
        "admin/account.html" if has_admin_access else "account.html",
        {
            "current_user": current_user,
            "role_names": role_names,
            "permissions": permissions,
            # Saved events, follows and alerts are all built; the page used to
            # advertise them as "coming soon".
            "saved_event_count": engagement.saved_event_count(db, user_id=current_user.id),
            "followed_cities": engagement.followed_cities(db, user_id=current_user.id),
            "display_name": current_user.full_name if display_name is None else display_name,
            "errors": errors or {},
            "edit_mode": edit_mode,
            "delete_confirmation_word": DELETE_CONFIRMATION_WORD,
            "email_available": email_delivery_available(),
        },
        status_code=status_code,
    )


@router.get("/account", response_class=HTMLResponse)
def account_page(
    request: Request,
    current_user: CurrentUser,
    db: DbSession,
    edit: bool = False,
):
    return _render_account(request, current_user, db, edit_mode=edit)


@router.post("/account", response_class=HTMLResponse)
async def update_account(
    request: Request,
    current_user: CurrentUser,
    db: DbSession,
    correlation_id: CorrelationId,
    ip_address: ClientIp,
    display_name: str = Form(""),
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)

    submitted_form = await request.form()
    if set(submitted_form) - _ACCOUNT_FORM_FIELDS:
        return _render_account(
            request,
            current_user,
            db,
            display_name=display_name,
            errors={"_global": "Unexpected account fields were submitted."},
            edit_mode=True,
            status_code=422,
        )

    normalized_name = display_name.strip()
    if not normalized_name:
        return _render_account(
            request,
            current_user,
            db,
            display_name=display_name,
            errors={"display_name": "Display name is required."},
            edit_mode=True,
            status_code=422,
        )
    if len(normalized_name) > _MAX_DISPLAY_NAME_LENGTH:
        return _render_account(
            request,
            current_user,
            db,
            display_name=display_name,
            errors={
                "display_name": (
                    f"Display name must be {_MAX_DISPLAY_NAME_LENGTH} characters or fewer."
                )
            },
            edit_mode=True,
            status_code=422,
        )

    previous_name = current_user.full_name
    if normalized_name != previous_name:
        current_user.full_name = normalized_name
        db.commit()
        record_audit(
            db,
            actor_id=current_user.id,
            action="display_name_changed",
            entity_type="user",
            entity_id=current_user.id,
            before={"display_name": previous_name},
            after={"display_name": normalized_name},
            correlation_id=correlation_id,
            ip_address=ip_address,
        )

    response = RedirectResponse(url="/account", status_code=303)
    set_flash(response, "Display name updated successfully.")
    return response


# Typed by an account that has no password (signed up with Google etc.), as
# its confirmation in place of one.
DELETE_CONFIRMATION_WORD = "DELETE"


@router.post("/account/delete", response_class=HTMLResponse)
def delete_own_account(
    request: Request,
    current_user: CurrentUser,
    db: DbSession,
    correlation_id: CorrelationId,
    ip_address: ClientIp,
    csrf_token: str = Form(...),
    password: str = Form(""),
    confirmation: str = Form(""),
):
    verify_csrf(request, csrf_token)

    if current_user.hashed_password:
        confirmed = bool(password) and verify_password(password, current_user.hashed_password)
        error = "That password is not correct."
    else:
        confirmed = confirmation.strip() == DELETE_CONFIRMATION_WORD
        error = f"Type {DELETE_CONFIRMATION_WORD} to confirm."
    if not confirmed:
        return _render_account(
            request, current_user, db, errors={"delete": error}, status_code=422
        )

    try:
        delete_account(db, current_user, correlation_id=correlation_id, ip_address=ip_address)
    except AppError:
        return _render_account(
            request,
            current_user,
            db,
            errors={
                "delete": (
                    "You are the only Super Administrator, so this account cannot be "
                    "deleted. Make someone else a Super Administrator first."
                )
            },
            status_code=403,
        )

    response = RedirectResponse(url="/", status_code=303)
    response.delete_cookie(get_settings().session_cookie_name, path="/")
    set_flash(response, "Your account and its data have been deleted.")
    return response


_VERIFICATION_RESENDS_PER_HOUR = 3


@router.post("/account/verify-email")
def resend_verification(
    request: Request,
    current_user: CurrentUser,
    db: DbSession,
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    response = RedirectResponse(url="/account", status_code=303)
    if current_user.email_verified_at is not None:
        set_flash(response, "Your email address is already confirmed.")
        return response
    if not email_delivery_available():
        set_flash(response, "Email isn't set up on this site yet, so no link can be sent.")
        return response
    if not get_rate_limit_backend().allow(
        f"verify-resend:{current_user.id}",
        limit=_VERIFICATION_RESENDS_PER_HOUR,
        window_seconds=3600,
    ):
        set_flash(response, "A link was sent recently. Please check your inbox and spam folder.")
        return response
    send_verification_email(db, current_user)
    set_flash(response, f"We've sent a confirmation link to {current_user.email}.")
    return response
