import json
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError

from app.core.csrf import verify_csrf
from app.core.exceptions import AppError, NotFoundError
from app.core.flash import set_flash
from app.core.onboarding import ALLOWED_TRANSITIONS, ONBOARDING_STATES, TRANSITION_PERMISSIONS
from app.core.templating import render
from app.dependencies import ClientIp, CorrelationId, CurrentUser, DbSession
from app.extraction.detection import MIN_PATTERN_CONFIDENCE
from app.extraction.inference.policy import READY_FOR_APPROVAL as INFERENCE_READY
from app.extraction.registry import REGISTRY
from app.models.event import Event
from app.models.user import User
from app.repositories.city import list_cities
from app.repositories.extraction_run import get_extraction_run, list_extraction_runs_for_website
from app.repositories.unsupported_site_report import list_reports_for_website
from app.repositories.website import (
    create_website,
    get_website,
    search_websites,
    update_website,
)
from app.schemas.extraction import SiteConfiguration
from app.schemas.extraction_config_forms import (
    REQUIRED_FIELD_CHOICES,
    ConfigFormInput,
    build_site_configuration,
    configuration_to_form,
)
from app.schemas.website import WebsiteCreate, WebsiteUpdate
from app.services.audit import record_audit
from app.services.extraction_runs import preview_extraction, run_detection, run_extraction
from app.services.onboarding_automation import detect_and_configure
from app.services.rbac import require_permission, user_has_permission
from app.services.website_configuration import (
    approve_configuration,
    reject_configuration,
    save_draft_configuration,
    select_pattern,
)
from app.services.websites import get_deletion_impact, transition_website

router = APIRouter(prefix="/admin/websites", tags=["admin-websites"])

ViewSites = Annotated[User, Depends(require_permission("sites.view"))]
CreateSites = Annotated[User, Depends(require_permission("sites.create"))]
UpdateSites = Annotated[User, Depends(require_permission("sites.update"))]
DeleteSites = Annotated[User, Depends(require_permission("sites.delete"))]
TestSites = Annotated[User, Depends(require_permission("sites.test"))]
ApproveSites = Annotated[User, Depends(require_permission("sites.approve"))]

PER_PAGE = 20


def _format_errors(exc: ValidationError) -> dict[str, str]:
    result: dict[str, str] = {}
    for err in exc.errors():
        field = ".".join(str(p) for p in err["loc"])
        result[field] = err["msg"]
    return result


def _parse_json_field(value: str) -> dict | None:
    value = value.strip()
    return json.loads(value) if value else None


def _build_website_data(
    *,
    name: str,
    source_display_name: str,
    city_id: str,
    base_url: str,
    event_listing_url: str,
    timezone_override: str,
    requires_js: bool,
    schedule_config: str,
    schema_cls: type[WebsiteCreate] | type[WebsiteUpdate],
):
    return schema_cls(
        name=name,
        source_display_name=source_display_name or None,
        city_id=int(city_id) if city_id else None,
        base_url=base_url,
        event_listing_url=event_listing_url or None,
        timezone_override=timezone_override or None,
        requires_js=requires_js,
        schedule_config=_parse_json_field(schedule_config),
    )


def _json_error_context() -> dict[str, str]:
    return {"schedule_config": "Must be valid JSON, or left blank"}


# --- List / filter -----------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
def list_websites_view(
    request: Request,
    current_user: ViewSites,
    db: DbSession,
    q: str | None = None,
    city_id: int | None = None,
    onboarding_status: str | None = None,
    page: int = 1,
):
    websites, total = search_websites(
        db,
        query=q,
        city_id=city_id,
        onboarding_status=onboarding_status,
        page=page,
        per_page=PER_PAGE,
    )
    cities = list_cities(db, active_only=False)
    base_url = (
        f"/admin/websites?q={q or ''}&city_id={city_id or ''}"
        f"&onboarding_status={onboarding_status or ''}"
    )
    return render(
        request,
        "admin/websites/list.html",
        {
            "current_user": current_user,
            "websites": websites,
            "total": total,
            "page": page,
            "per_page": PER_PAGE,
            "q": q or "",
            "city_id": city_id,
            "onboarding_status": onboarding_status or "",
            "cities": cities,
            "all_statuses": ONBOARDING_STATES,
            "base_url": base_url,
        },
    )


# --- Create ------------------------------------------------------------------------


@router.get("/new", response_class=HTMLResponse)
def new_website_form(request: Request, current_user: CreateSites, db: DbSession):
    cities = list_cities(db, active_only=False)
    return render(
        request,
        "admin/websites/form.html",
        {
            "current_user": current_user,
            "mode": "create",
            "website": None,
            "form": {"requires_js": False},
            "errors": {},
            "cities": cities,
        },
    )


@router.post("", response_class=HTMLResponse)
def create_website_view(
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    ip_address: ClientIp,
    current_user: CreateSites,
    name: str = Form(...),
    source_display_name: str = Form(""),
    city_id: str = Form(""),
    base_url: str = Form(...),
    event_listing_url: str = Form(""),
    timezone_override: str = Form(""),
    requires_js: str | None = Form(None),
    schedule_config: str = Form(""),
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    form_values = {
        "name": name,
        "source_display_name": source_display_name,
        "city_id": city_id,
        "base_url": base_url,
        "event_listing_url": event_listing_url,
        "timezone_override": timezone_override,
        "requires_js": requires_js is not None,
        "schedule_config": schedule_config,
    }
    cities = list_cities(db, active_only=False)

    try:
        data = _build_website_data(
            name=name,
            source_display_name=source_display_name,
            city_id=city_id,
            base_url=base_url,
            event_listing_url=event_listing_url,
            timezone_override=timezone_override,
            requires_js=requires_js is not None,
            schedule_config=schedule_config,
            schema_cls=WebsiteCreate,
        )
    except ValidationError as exc:
        return render(
            request,
            "admin/websites/form.html",
            {
                "current_user": current_user,
                "mode": "create",
                "website": None,
                "form": form_values,
                "errors": _format_errors(exc),
                "cities": cities,
            },
            status_code=422,
        )
    except ValueError:
        return render(
            request,
            "admin/websites/form.html",
            {
                "current_user": current_user,
                "mode": "create",
                "website": None,
                "form": form_values,
                "errors": _json_error_context(),
                "cities": cities,
            },
            status_code=422,
        )

    website = create_website(db, data)
    record_audit(
        db,
        actor_id=current_user.id,
        action="website_created",
        entity_type="website",
        entity_id=website.id,
        after=data.model_dump(mode="json"),
        correlation_id=correlation_id,
        ip_address=ip_address,
    )
    response = RedirectResponse(url=f"/admin/websites/{website.id}", status_code=303)
    set_flash(response, f"Website '{website.name}' created (draft).")
    return response


# --- Detail / edit -------------------------------------------------------------------


@router.get("/{website_id}", response_class=HTMLResponse)
def website_detail(website_id: int, request: Request, current_user: ViewSites, db: DbSession):
    website = get_website(db, website_id)
    if website is None:
        raise NotFoundError("Website not found")

    event_count = db.query(Event).filter(Event.website_id == website.id).count()
    next_states = sorted(
        s
        for s in ALLOWED_TRANSITIONS.get(website.onboarding_status, frozenset())
        if user_has_permission(db, current_user, TRANSITION_PERMISSIONS[s])
    )
    extraction_runs = list_extraction_runs_for_website(db, website.id, limit=20)
    latest_detection_run = next((r for r in extraction_runs if r.run_type == "detection"), None)
    latest_preview_run = next((r for r in extraction_runs if r.run_type == "preview"), None)
    detection_evidence = (website.proposed_pattern or {}).get("detection", {}).get("evidence", {})
    all_detector_results = (detection_evidence or {}).get("all_results", {})

    return render(
        request,
        "admin/websites/detail.html",
        {
            "current_user": current_user,
            "website": website,
            "event_count": event_count,
            "next_states": next_states,
            "can_update": user_has_permission(db, current_user, "sites.update"),
            "can_delete": user_has_permission(db, current_user, "sites.delete"),
            "can_approve": user_has_permission(db, current_user, "sites.approve"),
            "extraction_runs": extraction_runs,
            "unsupported_reports": list_reports_for_website(db, website.id, limit=20),
            "latest_detection_run": latest_detection_run,
            "latest_preview_run": latest_preview_run,
            "all_detector_results": all_detector_results,
            "pattern_names": REGISTRY.names(),
            "min_pattern_confidence": MIN_PATTERN_CONFIDENCE,
        },
    )


@router.get("/{website_id}/runs/{run_id}", response_class=HTMLResponse)
def extraction_run_detail(
    website_id: int, run_id: int, request: Request, current_user: ViewSites, db: DbSession
):
    website = get_website(db, website_id)
    if website is None:
        raise NotFoundError("Website not found")
    run = get_extraction_run(db, run_id)
    if run is None or run.website_id != website.id:
        raise NotFoundError("Extraction run not found")

    return render(
        request,
        "admin/websites/run_detail.html",
        {
            "current_user": current_user,
            "website": website,
            "run": run,
            "detector_evidence_json": json.dumps(run.detector_evidence, indent=2)
            if run.detector_evidence
            else None,
        },
    )


@router.get("/{website_id}/edit", response_class=HTMLResponse)
def edit_website_form(website_id: int, request: Request, current_user: UpdateSites, db: DbSession):
    website = get_website(db, website_id)
    if website is None:
        raise NotFoundError("Website not found")

    cities = list_cities(db, active_only=False)
    form_values = {
        "name": website.name,
        "source_display_name": website.source_display_name or "",
        "city_id": website.city_id or "",
        "base_url": website.base_url,
        "event_listing_url": website.event_listing_url or "",
        "timezone_override": website.timezone_override or "",
        "requires_js": website.requires_js,
        "schedule_config": json.dumps(website.schedule_config) if website.schedule_config else "",
    }
    return render(
        request,
        "admin/websites/form.html",
        {
            "current_user": current_user,
            "mode": "edit",
            "website": website,
            "form": form_values,
            "errors": {},
            "cities": cities,
        },
    )


@router.post("/{website_id}", response_class=HTMLResponse)
def update_website_view(
    website_id: int,
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    ip_address: ClientIp,
    current_user: UpdateSites,
    name: str = Form(...),
    source_display_name: str = Form(""),
    city_id: str = Form(""),
    base_url: str = Form(...),
    event_listing_url: str = Form(""),
    timezone_override: str = Form(""),
    requires_js: str | None = Form(None),
    schedule_config: str = Form(""),
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    website = get_website(db, website_id)
    if website is None:
        raise NotFoundError("Website not found")

    cities = list_cities(db, active_only=False)
    form_values = {
        "name": name,
        "source_display_name": source_display_name,
        "city_id": city_id,
        "base_url": base_url,
        "event_listing_url": event_listing_url,
        "timezone_override": timezone_override,
        "requires_js": requires_js is not None,
        "schedule_config": schedule_config,
    }

    try:
        data = _build_website_data(
            name=name,
            source_display_name=source_display_name,
            city_id=city_id,
            base_url=base_url,
            event_listing_url=event_listing_url,
            timezone_override=timezone_override,
            requires_js=requires_js is not None,
            schedule_config=schedule_config,
            schema_cls=WebsiteUpdate,
        )
    except ValidationError as exc:
        return render(
            request,
            "admin/websites/form.html",
            {
                "current_user": current_user,
                "mode": "edit",
                "website": website,
                "form": form_values,
                "errors": _format_errors(exc),
                "cities": cities,
            },
            status_code=422,
        )
    except ValueError:
        return render(
            request,
            "admin/websites/form.html",
            {
                "current_user": current_user,
                "mode": "edit",
                "website": website,
                "form": form_values,
                "errors": _json_error_context(),
                "cities": cities,
            },
            status_code=422,
        )

    before = {"name": website.name, "base_url": website.base_url, "city_id": website.city_id}
    update_website(db, website, data)
    record_audit(
        db,
        actor_id=current_user.id,
        action="website_updated",
        entity_type="website",
        entity_id=website.id,
        before=before,
        after=data.model_dump(mode="json"),
        correlation_id=correlation_id,
        ip_address=ip_address,
    )
    response = RedirectResponse(url=f"/admin/websites/{website.id}", status_code=303)
    set_flash(response, f"Website '{website.name}' updated.")
    return response


# --- Onboarding status transitions ---------------------------------------------------


@router.post("/{website_id}/status")
def change_website_status(
    website_id: int,
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    ip_address: ClientIp,
    current_user: CurrentUser,
    to_status: str = Form(...),
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    website = get_website(db, website_id)
    if website is None:
        raise NotFoundError("Website not found")

    if to_status not in ONBOARDING_STATES:
        raise AppError("Invalid target status", status_code=400)

    required_permission = TRANSITION_PERMISSIONS[to_status]
    if not user_has_permission(db, current_user, required_permission):
        raise AppError("Forbidden: missing permission", status_code=403)

    before = {"onboarding_status": website.onboarding_status, "is_active": website.is_active}
    transition_website(db, website, to_status)

    record_audit(
        db,
        actor_id=current_user.id,
        action="website_status_changed",
        entity_type="website",
        entity_id=website.id,
        before=before,
        after={"onboarding_status": website.onboarding_status, "is_active": website.is_active},
        correlation_id=correlation_id,
        ip_address=ip_address,
    )
    response = RedirectResponse(url=f"/admin/websites/{website.id}", status_code=303)
    set_flash(response, f"Website status changed to '{to_status}'.")
    return response


# --- Extraction pipeline actions ------------------------------------------------------
# Detection never persists events or touches approved_pattern. Preview never
# persists events. Persistent extraction requires an approved configuration.
# None of these change website activation — that stays the separate,
# explicit sites.activate transition below.


def _extraction_result_flash(response: RedirectResponse, action_label: str, result) -> None:
    summary = (
        f"{action_label}: {result.status} — {result.events_valid} valid / "
        f"{result.events_found} found, {result.events_rejected} rejected"
    )
    if result.pattern:
        summary += f" (pattern: {result.pattern})"
    level = "error" if result.status in ("failed", "blocked") else "success"
    set_flash(response, summary, level)


@router.post("/{website_id}/detect-pattern")
async def detect_pattern_view(
    website_id: int,
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    ip_address: ClientIp,
    current_user: TestSites,
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    website = get_website(db, website_id)
    if website is None:
        raise NotFoundError("Website not found")

    result = await run_detection(db, website, correlation_id=correlation_id)
    record_audit(
        db,
        actor_id=current_user.id,
        action="pattern_detection_requested",
        entity_type="website",
        entity_id=website.id,
        after={"status": result.status, "pattern": result.pattern, "run_id": result.run_id},
        correlation_id=correlation_id,
        ip_address=ip_address,
    )
    response = RedirectResponse(url=f"/admin/websites/{website.id}", status_code=303)
    _extraction_result_flash(response, "Pattern detection", result)
    return response


@router.post("/{website_id}/detect-and-configure")
async def detect_and_configure_view(
    website_id: int,
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    ip_address: ClientIp,
    current_user: TestSites,
    csrf_token: str = Form(...),
):
    """The main onboarding path: detect, infer a complete configuration, save
    it as a draft, preview it, and score the preview — one action. It never
    approves and never activates; those stay separate, permissioned steps."""
    verify_csrf(request, csrf_token)
    website = get_website(db, website_id)
    if website is None:
        raise NotFoundError("Website not found")

    result = await detect_and_configure(db, website, correlation_id=correlation_id)
    record_audit(
        db,
        actor_id=current_user.id,
        action="website_auto_configured",
        entity_type="website",
        entity_id=website.id,
        after={
            "outcome": result.outcome,
            "pattern": result.inference.pattern_name,
            "configuration_version": website.configuration_version,
            "preview_run_id": result.preview.run_id if result.preview else None,
            "valid_events": result.quality.valid_count if result.quality else 0,
        },
        correlation_id=correlation_id,
        ip_address=ip_address,
    )
    response = RedirectResponse(url=f"/admin/websites/{website.id}/onboarding", status_code=303)
    summary = f"Detect and configure: {result.outcome.replace('_', ' ')}"
    if result.quality is not None:
        summary += (
            f" — {result.quality.valid_count}/{result.quality.candidates_found} candidates valid"
        )
    level = "success" if result.outcome == INFERENCE_READY else "error"
    set_flash(response, summary, level)
    return response


@router.get("/{website_id}/onboarding", response_class=HTMLResponse)
def onboarding_result_view(
    website_id: int, request: Request, current_user: ViewSites, db: DbSession
):
    website = get_website(db, website_id)
    if website is None:
        raise NotFoundError("Website not found")

    inference = (website.proposed_pattern or {}).get("inference")
    return render(
        request,
        "admin/websites/onboarding.html",
        {
            "current_user": current_user,
            "website": website,
            "result": inference,
            "can_approve": user_has_permission(db, current_user, "sites.approve"),
            "can_update": user_has_permission(db, current_user, "sites.update"),
            "can_test": user_has_permission(db, current_user, "sites.test"),
            "ready_outcome": INFERENCE_READY,
        },
    )


@router.post("/{website_id}/preview-extraction")
async def preview_extraction_view(
    website_id: int,
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    ip_address: ClientIp,
    current_user: TestSites,
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    website = get_website(db, website_id)
    if website is None:
        raise NotFoundError("Website not found")

    result = await preview_extraction(db, website, correlation_id=correlation_id)
    record_audit(
        db,
        actor_id=current_user.id,
        action="extraction_preview_requested",
        entity_type="website",
        entity_id=website.id,
        after={
            "status": result.status,
            "events_found": result.events_found,
            "events_valid": result.events_valid,
            "events_rejected": result.events_rejected,
            "run_id": result.run_id,
        },
        correlation_id=correlation_id,
        ip_address=ip_address,
    )
    response = RedirectResponse(url=f"/admin/websites/{website.id}", status_code=303)
    _extraction_result_flash(response, "Preview", result)
    return response


@router.post("/{website_id}/approve-configuration")
def approve_configuration_view(
    website_id: int,
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    ip_address: ClientIp,
    current_user: ApproveSites,
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    website = get_website(db, website_id)
    if website is None:
        raise NotFoundError("Website not found")

    before = {"approved_pattern_set": bool(website.approved_pattern)}
    approve_configuration(db, website, approved_by_user_id=current_user.id)
    record_audit(
        db,
        actor_id=current_user.id,
        action="configuration_approved",
        entity_type="website",
        entity_id=website.id,
        before=before,
        after={"configuration_version": website.active_configuration_version},
        correlation_id=correlation_id,
        ip_address=ip_address,
    )
    response = RedirectResponse(url=f"/admin/websites/{website.id}", status_code=303)
    set_flash(response, f"Configuration approved (version {website.active_configuration_version}).")
    return response


@router.post("/{website_id}/run-extraction")
async def run_extraction_view(
    website_id: int,
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    ip_address: ClientIp,
    current_user: TestSites,
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    website = get_website(db, website_id)
    if website is None:
        raise NotFoundError("Website not found")

    result = await run_extraction(
        db, website, triggered_by_user_id=current_user.id, correlation_id=correlation_id
    )
    record_audit(
        db,
        actor_id=current_user.id,
        action="extraction_run_requested",
        entity_type="website",
        entity_id=website.id,
        after={
            "status": result.status,
            "events_inserted": result.events_inserted,
            "events_updated": result.events_updated,
            "duplicates_skipped": result.duplicates_skipped,
            "run_id": result.run_id,
        },
        correlation_id=correlation_id,
        ip_address=ip_address,
    )
    response = RedirectResponse(url=f"/admin/websites/{website.id}", status_code=303)
    _extraction_result_flash(response, "Extraction run", result)
    return response


# --- Configuration review --------------------------------------------------------------
# Structured, pattern-specific fields + independently-validated scoped JSON
# sub-editors for the inherently list/dict-shaped pieces — never one opaque
# textarea for the whole config. Saves only to the draft (`configuration`);
# approval is a separate, later action.


def _configure_context(
    request: Request,
    current_user: User,
    website,
    *,
    form,
    errors: dict[str, str],
) -> dict:
    return {
        "current_user": current_user,
        "website": website,
        "form": form,
        "errors": errors,
        "required_field_choices": REQUIRED_FIELD_CHOICES,
        "pattern_names": REGISTRY.names(),
    }


@router.get("/{website_id}/configure", response_class=HTMLResponse)
def configure_website_view(
    website_id: int, request: Request, current_user: UpdateSites, db: DbSession
):
    website = get_website(db, website_id)
    if website is None:
        raise NotFoundError("Website not found")

    form = None
    if website.configuration:
        form = configuration_to_form(SiteConfiguration.model_validate(website.configuration))
    elif website.proposed_pattern and website.proposed_pattern.get("configuration"):
        form = configuration_to_form(
            SiteConfiguration.model_validate(website.proposed_pattern["configuration"])
        )

    return render(
        request,
        "admin/websites/configure.html",
        _configure_context(request, current_user, website, form=form, errors={}),
    )


@router.post("/{website_id}/configure", response_class=HTMLResponse)
def configure_website_submit(
    website_id: int,
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    ip_address: ClientIp,
    current_user: UpdateSites,
    pattern_name: str = Form(...),
    listing_url: str = Form(""),
    api_endpoint: str = Form(""),
    timezone: str = Form(""),
    event_container_selector: str = Form(""),
    detail_page_selector: str = Form(""),
    max_detail_fetches: str = Form("25"),
    pagination_strategy: str = Form("none"),
    page_param: str = Form(""),
    page_size_param: str = Form(""),
    next_page_selector: str = Form(""),
    max_pages: str = Form("10"),
    max_events: str = Form("500"),
    date_formats: str = Form(""),
    time_formats: str = Form(""),
    required_fields: list[str] | None = Form(None),  # noqa: B008
    allow_page_url_as_canonical_fallback: str | None = Form(None),
    allow_offers_url_as_event_url: str | None = Form(None),
    field_selectors: str = Form(""),
    json_paths: str = Form(""),
    transformations: str = Form(""),
    category_mappings: str = Form(""),
    exclusion_rules: str = Form(""),
    geographic_filters: str = Form(""),
    query_params: str = Form(""),
    raw_json: str = Form(""),
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    website = get_website(db, website_id)
    if website is None:
        raise NotFoundError("Website not found")

    form_input = ConfigFormInput(
        pattern_name=pattern_name,
        listing_url=listing_url,
        api_endpoint=api_endpoint,
        timezone=timezone,
        event_container_selector=event_container_selector,
        detail_page_selector=detail_page_selector,
        max_detail_fetches=max_detail_fetches,
        pagination_strategy=pagination_strategy,
        page_param=page_param,
        page_size_param=page_size_param,
        next_page_selector=next_page_selector,
        max_pages=max_pages,
        max_events=max_events,
        date_formats=date_formats,
        time_formats=time_formats,
        required_fields=required_fields or [],
        allow_page_url_as_canonical_fallback=allow_page_url_as_canonical_fallback is not None,
        allow_offers_url_as_event_url=allow_offers_url_as_event_url is not None,
        field_selectors=field_selectors,
        json_paths=json_paths,
        transformations=transformations,
        category_mappings=category_mappings,
        exclusion_rules=exclusion_rules,
        geographic_filters=geographic_filters,
        query_params=query_params,
        raw_json=raw_json,
    )
    result = build_site_configuration(form_input)
    if result.configuration is None:
        return render(
            request,
            "admin/websites/configure.html",
            _configure_context(
                request, current_user, website, form=form_input, errors=result.errors
            ),
            status_code=422,
        )

    before_version = website.configuration_version
    save_draft_configuration(db, website, result.configuration)
    record_audit(
        db,
        actor_id=current_user.id,
        action="website_configuration_updated",
        entity_type="website",
        entity_id=website.id,
        before={"configuration_version": before_version},
        after={
            "configuration_version": website.configuration_version,
            "pattern_name": pattern_name,
        },
        correlation_id=correlation_id,
        ip_address=ip_address,
    )
    response = RedirectResponse(url=f"/admin/websites/{website.id}", status_code=303)
    set_flash(response, f"Configuration saved (draft version {website.configuration_version}).")
    return response


@router.post("/{website_id}/select-pattern")
def select_pattern_view(
    website_id: int,
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    ip_address: ClientIp,
    current_user: UpdateSites,
    pattern_name: str = Form(...),
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    website = get_website(db, website_id)
    if website is None:
        raise NotFoundError("Website not found")

    before = {"onboarding_status": website.onboarding_status}
    select_pattern(db, website, pattern_name=pattern_name)
    record_audit(
        db,
        actor_id=current_user.id,
        action="website_pattern_manually_selected",
        entity_type="website",
        entity_id=website.id,
        before=before,
        after={"onboarding_status": website.onboarding_status, "pattern_name": pattern_name},
        correlation_id=correlation_id,
        ip_address=ip_address,
    )
    response = RedirectResponse(url=f"/admin/websites/{website.id}/configure", status_code=303)
    set_flash(response, f"Pattern '{pattern_name}' selected — review and save the configuration.")
    return response


@router.post("/{website_id}/reject-configuration")
def reject_configuration_view(
    website_id: int,
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    ip_address: ClientIp,
    current_user: ApproveSites,
    reason: str = Form(...),
    notes: str = Form(""),
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    website = get_website(db, website_id)
    if website is None:
        raise NotFoundError("Website not found")

    before = {
        "onboarding_status": website.onboarding_status,
        "approved_pattern_set": bool(website.approved_pattern),
    }
    reject_configuration(db, website, reason=reason, correlation_id=correlation_id)
    detail = reason if not notes.strip() else f"{reason} | notes: {notes.strip()}"
    record_audit(
        db,
        actor_id=current_user.id,
        action="website_configuration_rejected",
        entity_type="website",
        entity_id=website.id,
        before=before,
        after={"onboarding_status": website.onboarding_status},
        detail=detail,
        correlation_id=correlation_id,
        ip_address=ip_address,
    )
    response = RedirectResponse(url=f"/admin/websites/{website.id}", status_code=303)
    set_flash(response, "Configuration rejected.")
    return response


# --- Deletion ------------------------------------------------------------------------


@router.get("/{website_id}/delete", response_class=HTMLResponse)
def website_delete_impact(
    website_id: int, request: Request, current_user: DeleteSites, db: DbSession
):
    website = get_website(db, website_id)
    if website is None:
        raise NotFoundError("Website not found")

    impact = get_deletion_impact(db, website)
    return render(
        request,
        "admin/websites/delete.html",
        {"current_user": current_user, "website": website, "impact": impact, "error": None},
    )


@router.post("/{website_id}/delete", response_class=HTMLResponse)
def delete_website_view(
    website_id: int,
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    ip_address: ClientIp,
    current_user: DeleteSites,
    confirm_name: str = Form(...),
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    website = get_website(db, website_id)
    if website is None:
        raise NotFoundError("Website not found")

    impact = get_deletion_impact(db, website)
    if not impact.can_delete:
        return render(
            request,
            "admin/websites/delete.html",
            {
                "current_user": current_user,
                "website": website,
                "impact": impact,
                "error": "Cannot delete: this website still has unarchived events.",
            },
            status_code=409,
        )

    if confirm_name.strip() != website.name:
        return render(
            request,
            "admin/websites/delete.html",
            {
                "current_user": current_user,
                "website": website,
                "impact": impact,
                "error": "Confirmation text did not match the website's name. Nothing was deleted.",
            },
            status_code=400,
        )

    before = {
        "name": website.name,
        "base_url": website.base_url,
        "archived_events": impact.archived_events,
    }
    deleted_id = website.id
    deleted_name = website.name
    db.delete(website)
    db.commit()
    record_audit(
        db,
        actor_id=current_user.id,
        action="website_deleted",
        entity_type="website",
        entity_id=deleted_id,
        before=before,
        correlation_id=correlation_id,
        ip_address=ip_address,
    )
    response = RedirectResponse(url="/admin/websites", status_code=303)
    set_flash(response, f"Website '{deleted_name}' permanently deleted.")
    return response
