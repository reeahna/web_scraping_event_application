from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.core.csrf import verify_csrf
from app.core.flash import set_flash
from app.core.templating import render
from app.dependencies import ClientIp, CorrelationId, CurrentUser, DbSession
from app.services.audit import record_audit
from app.services.rbac import can_access_admin

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
    return render(
        request,
        "admin/account.html" if has_admin_access else "account.html",
        {
            "current_user": current_user,
            "role_names": role_names,
            "display_name": current_user.full_name if display_name is None else display_name,
            "errors": errors or {},
            "edit_mode": edit_mode,
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
