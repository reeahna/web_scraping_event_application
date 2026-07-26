"""Policy administration and decision history.

Every route is gated by `settings.manage` (creating, editing, activating and
assigning) or `sites.view` (reading decision history), enforced by dependency
rather than by hiding a control. Forms are structured inputs — there is no
raw-JSON path into a policy.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.core.csrf import verify_csrf
from app.core.exceptions import AppError, NotFoundError
from app.core.flash import set_flash
from app.core.templating import render
from app.dependencies import ClientIp, CorrelationId, DbSession
from app.extraction.registry import REGISTRY
from app.models.auto_onboarding_policy import AutoOnboardingPolicy
from app.models.role import Role
from app.models.user import User
from app.repositories.auto_onboarding import (
    count_decisions_for_policy,
    get_decision,
    get_policy,
    list_decisions_for_policy,
    list_policies,
    policy_city_ids,
    policy_role_ids,
)
from app.repositories.city import list_cities
from app.services.auto_onboarding_execution import effective_decision
from app.services.auto_onboarding_policies import (
    ACTIVATION_CONFIRMATION_PHRASE,
    APPROVAL_CONFIRMATION_PHRASE,
    BOOLEAN_FIELDS,
    COUNT_FIELDS,
    PERCENTAGE_FIELDS,
    VERSIONED_FIELDS,
    assign_city,
    create_policy,
    remove_city,
    set_active,
    set_global_default,
    set_roles,
    update_policy,
)
from app.services.rbac import require_permission

router = APIRouter(prefix="/admin/settings/onboarding-policies", tags=["admin-auto-onboarding"])

ManageSettings = Annotated[User, Depends(require_permission("settings.manage"))]
ViewSites = Annotated[User, Depends(require_permission("sites.view"))]

# Rendered as grouped sections so the form reads as decisions rather than as
# forty anonymous numbers.
FORM_SECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Workflow enablement",
        (
            "automatic_configuration_enabled",
            "automatic_preview_enabled",
            "automatic_approval_enabled",
            "automatic_activation_enabled",
        ),
    ),
    (
        "Configuration origin and rendering",
        (
            "allow_generic_html_cards",
            "allow_browser_required",
            "allow_ai_origin",
            "allow_administrator_manual_origin",
            "allow_imported_configuration",
            "allow_detail_page_enrichment",
        ),
    ),
    (
        "General quality thresholds",
        (
            "minimum_detector_confidence",
            "minimum_events_found",
            "minimum_valid_events",
            "minimum_valid_percentage",
            "maximum_rejected_percentage",
            "minimum_canonical_url_coverage",
            "minimum_start_date_coverage",
            "minimum_start_date_parse_success",
            "maximum_duplicate_rate",
            "maximum_warning_count",
            "maximum_critical_warning_count",
            "minimum_distinct_events",
            "require_absolute_public_urls",
            "require_zero_critical_warnings",
            "require_distinct_events",
        ),
    ),
    (
        "Shared date-range and geographic quality",
        (
            "require_date_range_parse_success",
            "minimum_date_range_parse_success",
            "require_geographic_filter",
            "minimum_geographic_inclusion_rate",
        ),
    ),
    (
        "Generic HTML thresholds (stricter)",
        (
            "generic_html_minimum_detector_confidence",
            "generic_html_minimum_events_found",
            "generic_html_minimum_valid_events",
            "generic_html_minimum_valid_percentage",
            "generic_html_maximum_rejected_percentage",
            "generic_html_minimum_required_field_confidence",
            "generic_html_minimum_required_field_coverage",
            "generic_html_minimum_date_format_confidence",
            "generic_html_minimum_distinct_event_count",
            "generic_html_reject_broad_canonical_selector",
            "generic_html_reject_unstable_required_selectors",
        ),
    ),
)


def _policy_or_404(db, policy_id: int) -> AutoOnboardingPolicy:
    policy = get_policy(db, policy_id)
    if policy is None:
        raise NotFoundError("Automatic-onboarding policy not found")
    return policy


async def _values_from_form(request: Request) -> tuple[dict, dict, str, str]:
    """Reads the structured form. Unexpected fields are rejected rather than
    ignored, and nothing is mass-assigned: only names in VERSIONED_FIELDS are
    ever read."""
    form = await request.form()
    allowed = {
        "csrf_token",
        "name",
        "description",
        "confirm_approval",
        "confirm_activation",
        "allowed_pattern_names",
        "role_ids",
        *VERSIONED_FIELDS,
    }
    unexpected = set(form) - allowed
    if unexpected:
        raise AppError(
            f"Unexpected form fields were submitted: {', '.join(sorted(unexpected))}",
            status_code=422,
        )

    values: dict = {}
    for field in VERSIONED_FIELDS:
        if field == "allowed_pattern_names":
            continue
        if field in BOOLEAN_FIELDS:
            values[field] = field in form
        elif field in form and str(form[field]).strip():
            values[field] = form[field]
    values["allowed_pattern_names"] = form.getlist("allowed_pattern_names")

    confirmations = {
        "approval": str(form.get("confirm_approval", "")).strip(),
        "activation": str(form.get("confirm_activation", "")).strip(),
    }
    return values, confirmations, str(form.get("name", "")), str(form.get("description", ""))


def _form_context(db, request: Request, current_user, policy=None, **extra) -> dict:
    return {
        "current_user": current_user,
        "policy": policy,
        "sections": FORM_SECTIONS,
        "percentage_fields": PERCENTAGE_FIELDS,
        "count_fields": COUNT_FIELDS,
        "boolean_fields": BOOLEAN_FIELDS,
        "pattern_names": REGISTRY.names(),
        "cities": list_cities(db, active_only=False),
        "roles": db.query(Role).order_by(Role.name).all(),
        "assigned_city_ids": policy_city_ids(db, policy.id) if policy else [],
        "assigned_role_ids": policy_role_ids(db, policy.id) if policy else [],
        "approval_phrase": APPROVAL_CONFIRMATION_PHRASE,
        "activation_phrase": ACTIVATION_CONFIRMATION_PHRASE,
        **extra,
    }


# --- list / detail -----------------------------------------------------------


@router.get("", response_class=HTMLResponse)
def policy_list(request: Request, current_user: ManageSettings, db: DbSession):
    policies = list_policies(db)
    return render(
        request,
        "admin/auto_onboarding/policies.html",
        {
            "current_user": current_user,
            "policies": policies,
            "decision_counts": {p.id: count_decisions_for_policy(db, p.id) for p in policies},
            "city_counts": {p.id: len(policy_city_ids(db, p.id)) for p in policies},
        },
    )


@router.get("/new", response_class=HTMLResponse)
def new_policy_form(request: Request, current_user: ManageSettings, db: DbSession):
    return render(
        request,
        "admin/auto_onboarding/policy_form.html",
        _form_context(db, request, current_user, policy=None, mode="create", errors={}),
    )


@router.post("", response_class=HTMLResponse)
async def create_policy_view(
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    ip_address: ClientIp,
    current_user: ManageSettings,
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    values, confirmations, name, description = await _values_from_form(request)
    policy = create_policy(
        db,
        name=name,
        description=description,
        values=values,
        confirmations=confirmations,
        actor_id=current_user.id,
        correlation_id=correlation_id,
    )
    response = RedirectResponse(
        url=f"/admin/settings/onboarding-policies/{policy.id}", status_code=303
    )
    set_flash(response, f"Policy '{policy.name}' created.", "success")
    return response


@router.get("/{policy_id}", response_class=HTMLResponse)
def policy_detail(
    policy_id: int, request: Request, current_user: ManageSettings, db: DbSession
):
    policy = _policy_or_404(db, policy_id)
    decisions = list_decisions_for_policy(db, policy.id, limit=25)
    return render(
        request,
        "admin/auto_onboarding/policy_detail.html",
        _form_context(
            db,
            request,
            current_user,
            policy=policy,
            decisions=decisions,
            effective={d.id: effective_decision(d) for d in decisions},
            decision_count=count_decisions_for_policy(db, policy.id),
        ),
    )


@router.get("/{policy_id}/edit", response_class=HTMLResponse)
def edit_policy_form(
    policy_id: int, request: Request, current_user: ManageSettings, db: DbSession
):
    policy = _policy_or_404(db, policy_id)
    return render(
        request,
        "admin/auto_onboarding/policy_form.html",
        _form_context(db, request, current_user, policy=policy, mode="edit", errors={}),
    )


@router.post("/{policy_id}", response_class=HTMLResponse)
async def update_policy_view(
    policy_id: int,
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    current_user: ManageSettings,
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    policy = _policy_or_404(db, policy_id)
    values, confirmations, name, description = await _values_from_form(request)
    change = update_policy(
        db,
        policy,
        name=name,
        description=description,
        values=values,
        confirmations=confirmations,
        actor_id=current_user.id,
        correlation_id=correlation_id,
    )
    response = RedirectResponse(
        url=f"/admin/settings/onboarding-policies/{policy.id}", status_code=303
    )
    message = f"Policy '{policy.name}' saved."
    if change.version_bumped:
        message += f" Version is now {policy.version}; existing decisions are unchanged."
    set_flash(response, message, "success")
    return response


# --- state and assignment -----------------------------------------------------


@router.post("/{policy_id}/status")
def change_policy_status(
    policy_id: int,
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    current_user: ManageSettings,
    active: str = Form(...),
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    policy = _policy_or_404(db, policy_id)
    set_active(
        db,
        policy,
        active=active == "true",
        actor_id=current_user.id,
        correlation_id=correlation_id,
    )
    response = RedirectResponse(
        url=f"/admin/settings/onboarding-policies/{policy.id}", status_code=303
    )
    state = "active" if policy.active else "inactive"
    set_flash(response, f"Policy '{policy.name}' is now {state}.")
    return response


@router.post("/{policy_id}/global-default")
def assign_global_default(
    policy_id: int,
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    current_user: ManageSettings,
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    policy = _policy_or_404(db, policy_id)
    set_global_default(db, policy, actor_id=current_user.id, correlation_id=correlation_id)
    response = RedirectResponse(
        url=f"/admin/settings/onboarding-policies/{policy.id}", status_code=303
    )
    set_flash(response, f"'{policy.name}' is now the global default policy.", "success")
    return response


@router.post("/{policy_id}/cities")
def assign_city_view(
    policy_id: int,
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    current_user: ManageSettings,
    city_id: str = Form(...),
    action: str = Form("assign"),
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    policy = _policy_or_404(db, policy_id)
    if action == "remove":
        remove_city(
            db,
            policy,
            city_id=int(city_id),
            actor_id=current_user.id,
            correlation_id=correlation_id,
        )
        message = "City assignment removed."
    else:
        assign_city(
            db,
            policy,
            city_id=int(city_id),
            actor_id=current_user.id,
            correlation_id=correlation_id,
        )
        message = "City assigned to this policy."
    response = RedirectResponse(
        url=f"/admin/settings/onboarding-policies/{policy.id}", status_code=303
    )
    set_flash(response, message, "success")
    return response


@router.post("/{policy_id}/roles")
async def set_roles_view(
    policy_id: int,
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    current_user: ManageSettings,
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    policy = _policy_or_404(db, policy_id)
    form = await request.form()
    role_ids = [int(value) for value in form.getlist("role_ids")]
    set_roles(
        db, policy, role_ids=role_ids, actor_id=current_user.id, correlation_id=correlation_id
    )
    response = RedirectResponse(
        url=f"/admin/settings/onboarding-policies/{policy.id}", status_code=303
    )
    set_flash(response, "Role restrictions updated.", "success")
    return response


# --- decision history ---------------------------------------------------------


decisions_router = APIRouter(prefix="/admin/onboarding/decisions", tags=["admin-auto-onboarding"])


@decisions_router.get("/{decision_id}", response_class=HTMLResponse)
def decision_detail(
    decision_id: int, request: Request, current_user: ViewSites, db: DbSession
):
    decision = get_decision(db, decision_id)
    if decision is None:
        raise NotFoundError("Automatic-onboarding decision not found")
    return render(
        request,
        "admin/auto_onboarding/decision_detail.html",
        {
            "current_user": current_user,
            "decision": decision,
            "effective": effective_decision(decision),
            "results": decision.action_results,
        },
    )
