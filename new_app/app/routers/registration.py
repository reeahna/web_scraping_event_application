from datetime import UTC, datetime

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError

from app.config import get_settings
from app.core.csrf import verify_csrf
from app.core.exceptions import AppError
from app.core.templating import render
from app.dependencies import ClientIp, CorrelationId, DbSession
from app.schemas.registration import RegistrationCreate
from app.services.audit import record_audit
from app.services.auth import create_session
from app.services.rate_limit import check_registration_rate_limit
from app.services.registration import EmailAlreadyRegisteredError, register_user

router = APIRouter(tags=["registration"])


def _format_errors(exc: ValidationError) -> dict[str, str]:
    result: dict[str, str] = {}
    for err in exc.errors():
        field = ".".join(str(p) for p in err["loc"])
        result[field] = err["msg"]
    return result


def _render_register(
    request: Request,
    *,
    form: dict | None = None,
    errors: dict | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    settings = get_settings()
    return render(
        request,
        "register.html",
        {
            "registration_enabled": settings.registration_enabled,
            "form": form or {},
            "errors": errors or {},
        },
        status_code=status_code,
    )


@router.get("/register", response_class=HTMLResponse)
def register_form(request: Request):
    settings = get_settings()
    return _render_register(request, status_code=200 if settings.registration_enabled else 404)


@router.post("/register", response_class=HTMLResponse)
async def register_submit(
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    ip_address: ClientIp,
    display_name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    settings = get_settings()
    form_values = {"display_name": display_name, "email": email}

    if not settings.registration_enabled:
        return _render_register(request, form=form_values, status_code=404)

    # Individually declared FastAPI Form fields normally ignore unknown keys.
    # Reject them explicitly so role/permission/account-state injection is a
    # failed request, matching the dedicated schema's closed-field contract.
    submitted_form = await request.form()
    allowed_fields = {
        "display_name",
        "email",
        "password",
        "password_confirm",
        "csrf_token",
    }
    if set(submitted_form) - allowed_fields:
        return _render_register(
            request,
            form=form_values,
            errors={"_global": "Unexpected registration fields were submitted."},
            status_code=422,
        )

    try:
        check_registration_rate_limit(ip_address)
    except AppError as exc:
        return _render_register(
            request,
            form=form_values,
            errors={"_global": exc.message},
            status_code=exc.status_code,
        )

    try:
        data = RegistrationCreate(
            display_name=display_name,
            email=email,
            password=password,
            password_confirm=password_confirm,
        )
    except ValidationError as exc:
        return _render_register(
            request, form=form_values, errors=_format_errors(exc), status_code=422
        )

    try:
        user = register_user(db, data, correlation_id=correlation_id, ip_address=ip_address)
    except EmailAlreadyRegisteredError as exc:
        return _render_register(
            request,
            form=form_values,
            errors={"email": exc.message},
            status_code=exc.status_code,
        )

    # Immediate login after registration: a brand-new session, created the
    # same way (and with the same cookie attributes) as a normal login — no
    # session fixation, since this token didn't exist before this request.
    raw_token = create_session(db, user, request)
    user.last_login_at = datetime.now(UTC)
    db.commit()
    record_audit(
        db,
        actor_id=user.id,
        action="login_after_registration",
        entity_type="user",
        entity_id=user.id,
        correlation_id=correlation_id,
        ip_address=ip_address,
    )

    response = RedirectResponse(url="/account", status_code=303)
    response.set_cookie(
        settings.session_cookie_name,
        raw_token,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
        max_age=settings.session_ttl_seconds,
    )
    return response
