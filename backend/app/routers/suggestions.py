from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.core.csrf import verify_csrf
from app.core.exceptions import AppError, NotFoundError
from app.core.flash import set_flash
from app.core.forms import OptionalText
from app.core.templating import render
from app.dependencies import (
    ClientIp,
    CorrelationId,
    CurrentUser,
    DbSession,
    OptionalCurrentUser,
)
from app.models.suggestion import Suggestion
from app.models.user import User
from app.services import suggestions as svc
from app.services.audit import record_audit
from app.services.rate_limit import get_rate_limit_backend
from app.services.rbac import require_permission, user_has_permission

router = APIRouter(tags=["suggestions"])

ViewSuggestions = Annotated[User, Depends(require_permission("reports.view"))]
ManageSuggestions = Annotated[User, Depends(require_permission("reports.manage"))]

ADMIN_PER_PAGE = 25

_FEEDBACK_THANKS = {
    svc.KIND_FEATURE: "Thanks! Your idea has been sent to the team.",
    svc.KIND_BUG: "Thanks for letting us know! Your report has been sent to the team.",
}


def _render_suggest(
    request: Request,
    current_user: User,
    db: DbSession,
    *,
    form: svc.SuggestionInput,
    status_code: int = 200,
) -> HTMLResponse:
    return render(
        request,
        "suggest.html",
        {
            "current_user": current_user,
            "form": form,
            "errors": form.errors,
            "mine": svc.list_for_user(db, current_user.id),
            "statuses": svc.SUGGESTION_STATUSES,
            "status_label": svc.status_label,
            "limits": {
                "title": svc.TITLE_MAX,
                "region": svc.REGION_MAX,
                "university": svc.UNIVERSITY_MAX,
                "details": svc.DETAILS_MAX,
            },
        },
        status_code=status_code,
    )


# The hidden "website" field on /feedback. People never see or fill it; form
# bots that fill every field do, and their message is dropped.
HONEYPOT_FIELD = "website"

_LIMIT_MESSAGE = "You've sent a lot of suggestions today. Thank you! Please try again tomorrow."
_ANON_IP_LIMIT_MESSAGE = (
    "You've sent a lot of messages in the last hour. Thank you! Please try again a "
    "little later, or sign in to send more."
)


def _render_feedback(
    request: Request,
    current_user: User | None,
    db: DbSession,
    *,
    form: svc.SuggestionInput,
    status_code: int = 200,
) -> HTMLResponse:
    return render(
        request,
        "feedback.html",
        {
            "current_user": current_user,
            "form": form,
            "errors": form.errors,
            "mine": (
                svc.list_for_user(db, current_user.id, kinds=svc.FEEDBACK_KINDS)
                if current_user
                else []
            ),
            "status_label": svc.status_label,
            "kind_labels": svc.KIND_LABELS,
            "honeypot_field": HONEYPOT_FIELD,
            "limits": {
                "title": svc.TITLE_MAX,
                "details": svc.DETAILS_MAX,
                "email": svc.EMAIL_MAX,
            },
        },
        status_code=status_code,
    )


@router.get("/suggest", response_class=HTMLResponse)
def suggest_page(
    request: Request, current_user: CurrentUser, db: DbSession, kind: OptionalText = None
):
    # Feature ideas now live on the "New features / bug fixes" page.
    if kind == svc.KIND_FEATURE:
        return RedirectResponse(url="/feedback?kind=feature", status_code=303)
    form = svc.SuggestionInput(kind=svc.KIND_TOWN)
    return _render_suggest(request, current_user, db, form=form)


@router.post("/suggest", response_class=HTMLResponse)
def submit_suggestion(
    request: Request,
    current_user: CurrentUser,
    db: DbSession,
    correlation_id: CorrelationId,
    kind: str = Form(svc.KIND_TOWN),
    title: str = Form(""),
    region: str = Form(""),
    university: str = Form(""),
    details: str = Form(""),
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    form = svc.parse_suggestion(
        kind=kind, title=title, region=region, university=university, details=details
    )
    if not form.is_valid:
        return _render_suggest(request, current_user, db, form=form, status_code=422)
    try:
        svc.create_suggestion(db, current_user, form, correlation_id=correlation_id)
    except svc.SuggestionLimitError:
        form.errors["form"] = _LIMIT_MESSAGE
        return _render_suggest(request, current_user, db, form=form, status_code=429)
    if form.kind == svc.KIND_TOWN:
        response = RedirectResponse(url="/suggest?kind=town", status_code=303)
        set_flash(response, f"Thanks! We'll look into adding {form.title}.")
    else:
        response = RedirectResponse(url=f"/feedback?kind={form.kind}", status_code=303)
        set_flash(response, _FEEDBACK_THANKS[form.kind])
    return response


@router.get("/feedback", response_class=HTMLResponse)
def feedback_page(
    request: Request, current_user: OptionalCurrentUser, db: DbSession, kind: OptionalText = None
):
    form = svc.SuggestionInput(
        kind=kind if kind in svc.FEEDBACK_KINDS else svc.KIND_FEATURE
    )
    return _render_feedback(request, current_user, db, form=form)


@router.post("/feedback", response_class=HTMLResponse)
def submit_feedback(
    request: Request,
    current_user: OptionalCurrentUser,
    db: DbSession,
    correlation_id: CorrelationId,
    ip_address: ClientIp,
    kind: str = Form(svc.KIND_FEATURE),
    title: str = Form(""),
    details: str = Form(""),
    contact_email: str = Form(""),
    website: str = Form(""),  # the honeypot, see HONEYPOT_FIELD
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    # Only features and bugs are sent from here; anything else is a feature.
    kind = kind if kind in svc.FEEDBACK_KINDS else svc.KIND_FEATURE
    thanks = RedirectResponse(url=f"/feedback?kind={kind}", status_code=303)
    set_flash(thanks, _FEEDBACK_THANKS[kind])

    # A bot that filled the hidden field gets the same thanks a person does,
    # so it learns nothing, and nothing is saved or sent to the team.
    if website:
        return thanks

    form = svc.parse_suggestion(
        kind=kind,
        title=title,
        region="",
        university="",
        details=details,
        # A signed-in visitor already has an address on their account.
        contact_email="" if current_user else contact_email,
    )
    if not form.is_valid:
        return _render_feedback(request, current_user, db, form=form, status_code=422)

    # Per address for visitors without an account: the connection's address as
    # the rest of the app sees it (ClientIp), never a raw X-Forwarded-For value
    # a sender can write themselves.
    if (
        current_user is None
        and ip_address
        and not get_rate_limit_backend().allow(
            f"feedback:{ip_address}",
            limit=svc.ANONYMOUS_PER_IP_PER_HOUR,
            window_seconds=60 * 60,
        )
    ):
        form.errors["form"] = _ANON_IP_LIMIT_MESSAGE
        return _render_feedback(request, current_user, db, form=form, status_code=429)
    try:
        svc.create_suggestion(db, current_user, form, correlation_id=correlation_id)
    except svc.SuggestionLimitError:
        form.errors["form"] = _LIMIT_MESSAGE
        return _render_feedback(request, current_user, db, form=form, status_code=429)
    return thanks


@router.get("/admin/suggestions", response_class=HTMLResponse)
def admin_suggestions(
    request: Request,
    current_user: ViewSuggestions,
    db: DbSession,
    kind: OptionalText = None,
    status: OptionalText = None,
    page: int = 1,
):
    rows, total = svc.list_suggestions(
        db, kind=kind, status=status, page=page, per_page=ADMIN_PER_PAGE
    )
    return render(
        request,
        "admin/suggestions/list.html",
        {
            "current_user": current_user,
            "suggestions": rows,
            "total": total,
            "page": max(page, 1),
            "per_page": ADMIN_PER_PAGE,
            "kind": kind if kind in svc.SUGGESTION_KINDS else "",
            "status": status if status in svc.SUGGESTION_STATUSES else "",
            "statuses": svc.SUGGESTION_STATUSES,
            "kind_labels": svc.KIND_LABELS,
            "status_label": svc.status_label,
            "base_url": f"/admin/suggestions?kind={kind or ''}&status={status or ''}",
            "can_manage": user_has_permission(db, current_user, "reports.manage"),
        },
    )


@router.post("/admin/suggestions/{suggestion_id}")
def admin_update_suggestion(
    suggestion_id: int,
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    ip_address: ClientIp,
    current_user: ManageSuggestions,
    status: str = Form(...),
    admin_notes: str = Form(""),
    back: str = Form(""),
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    suggestion = db.get(Suggestion, suggestion_id)
    if suggestion is None:
        raise NotFoundError("Suggestion not found")
    if status not in svc.SUGGESTION_STATUSES:
        raise AppError("Choose a valid status.", status_code=422)
    before = {"status": suggestion.status, "admin_notes": suggestion.admin_notes}
    svc.update_suggestion(db, suggestion, status=status, admin_notes=admin_notes)
    record_audit(
        db,
        actor_id=current_user.id,
        action="suggestion_updated",
        entity_type="suggestion",
        entity_id=suggestion.id,
        before=before,
        after={"status": suggestion.status, "admin_notes": suggestion.admin_notes},
        correlation_id=correlation_id,
        ip_address=ip_address,
    )
    # Return to the same filtered list; only ever a path on this page.
    target = back if back.startswith("/admin/suggestions") else "/admin/suggestions"
    response = RedirectResponse(url=target, status_code=303)
    set_flash(response, "Suggestion updated.")
    return response
