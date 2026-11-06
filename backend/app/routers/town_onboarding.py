"""Onboarding a whole college town with Claude (app.services.town_onboarding).

Submitting needs sites.create and cities.create, because a finished request
creates a city and queues its sources. Viewing needs sites.view.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from app.config import get_settings
from app.core.csrf import verify_csrf
from app.core.exceptions import AppError, NotFoundError
from app.core.flash import set_flash
from app.core.templating import render
from app.dependencies import DbSession
from app.models.town_onboarding import TOWN_FAILED, TownOnboardingRequest
from app.models.user import User
from app.services.rbac import require_permission, user_has_permission
from app.services.town_onboarding import (
    list_requests,
    proposal_csv,
    proposal_from_request,
    queue_town,
    retry_town,
)

router = APIRouter(prefix="/admin/onboarding/towns", tags=["admin-town-onboarding"])

ViewSites = Annotated[User, Depends(require_permission("sites.view"))]
CreateSites = Annotated[User, Depends(require_permission("sites.create"))]


def _require_city_create(db, user: User) -> None:
    if not user_has_permission(db, user, "cities.create"):
        raise AppError("Forbidden: missing permission", status_code=403)


def _get(db, request_id: int) -> TownOnboardingRequest:
    row = db.get(TownOnboardingRequest, request_id)
    if row is None:
        raise NotFoundError("Town onboarding request not found")
    return row


def _context(request: Request, current_user: User, db, **extra) -> dict:
    settings = get_settings()
    return {
        "current_user": current_user,
        "requests": list_requests(db),
        "configured": bool(settings.anthropic_api_key),
        "min_sources": settings.town_onboarding_min_sources,
        "min_per_scope": settings.town_onboarding_min_per_scope,
        "can_create": user_has_permission(db, current_user, "sites.create")
        and user_has_permission(db, current_user, "cities.create"),
        "errors": {},
        "form": {},
        **extra,
    }


@router.get("", response_class=HTMLResponse)
def town_list(request: Request, current_user: ViewSites, db: DbSession):
    return render(request, "admin/onboarding/towns.html", _context(request, current_user, db))


@router.post("", response_class=HTMLResponse)
def town_submit(
    request: Request,
    db: DbSession,
    current_user: CreateSites,
    town_name: str = Form(""),
    state_or_region: str = Form(""),
    universities: str = Form(""),
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    _require_city_create(db, current_user)
    form = {
        "town_name": town_name,
        "state_or_region": state_or_region,
        "universities": universities,
    }
    errors: dict[str, str] = {}
    if not town_name.strip():
        errors["town_name"] = "Enter the town's name."
    elif len(town_name.strip()) > 255:
        errors["town_name"] = "The town's name is too long."
    if len(state_or_region.strip()) > 100:
        errors["state_or_region"] = "The state is too long."
    if not get_settings().anthropic_api_key:
        errors["town_name"] = "Set ANTHROPIC_API_KEY to onboard towns with Claude."
    if errors:
        return render(
            request,
            "admin/onboarding/towns.html",
            _context(request, current_user, db, errors=errors, form=form),
            status_code=422,
        )

    names = [line.strip() for line in universities.replace(";", "\n").splitlines()]
    row = queue_town(
        db,
        town_name=town_name,
        state_or_region=state_or_region,
        universities=[name[:255] for name in names if name][:10],
        requested_by_user_id=current_user.id,
    )
    response = RedirectResponse(url=f"/admin/onboarding/towns/{row.id}", status_code=303)
    set_flash(
        response,
        f"Queued {row.town_name}. Claude usually takes a few minutes to find its sources.",
    )
    return response


@router.get("/{request_id}", response_class=HTMLResponse)
def town_detail(request_id: int, request: Request, current_user: ViewSites, db: DbSession):
    row = _get(db, request_id)
    proposal = proposal_from_request(row)
    return render(
        request,
        "admin/onboarding/town_detail.html",
        {
            "current_user": current_user,
            "town": row,
            "proposal": proposal,
            "campus": [s for s in proposal.sources if s.scope == "campus"] if proposal else [],
            "in_town": [s for s in proposal.sources if s.scope == "town"] if proposal else [],
            "can_retry": row.status == TOWN_FAILED
            and user_has_permission(db, current_user, "sites.create")
            and user_has_permission(db, current_user, "cities.create"),
        },
    )


@router.get("/{request_id}/sources.csv")
def town_csv(request_id: int, current_user: ViewSites, db: DbSession):
    row = _get(db, request_id)
    proposal = proposal_from_request(row)
    if proposal is None:
        raise NotFoundError("Claude has not proposed sources for this town yet")
    return PlainTextResponse(
        proposal_csv(proposal),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{proposal.slug}.csv"'},
    )


@router.post("/{request_id}/retry")
def town_retry(
    request_id: int,
    request: Request,
    db: DbSession,
    current_user: CreateSites,
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    _require_city_create(db, current_user)
    row = retry_town(db, _get(db, request_id))
    response = RedirectResponse(url=f"/admin/onboarding/towns/{row.id}", status_code=303)
    set_flash(response, f"Queued {row.town_name} again.")
    return response
