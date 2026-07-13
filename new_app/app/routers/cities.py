import json
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError

from app.core.csrf import verify_csrf
from app.core.exceptions import AppError, NotFoundError
from app.core.flash import set_flash
from app.core.forms import reject_unexpected_form_fields
from app.core.templating import render
from app.dependencies import ClientIp, CorrelationId, DbSession
from app.models.event import Event
from app.models.user import User
from app.models.website import Website
from app.repositories.city import (
    create_city,
    get_city,
    get_city_by_slug,
    search_cities,
    update_city,
)
from app.schemas.city import CityCreate, CityUpdate
from app.services.audit import record_audit
from app.services.cities import (
    archive_city_events,
    archive_city_websites,
    delete_archived_city_events,
    get_deletion_impact,
)
from app.services.rbac import require_permission, user_has_permission

router = APIRouter(prefix="/admin/cities", tags=["admin-cities"])

ViewCities = Annotated[User, Depends(require_permission("cities.view"))]
CreateCities = Annotated[User, Depends(require_permission("cities.create"))]
UpdateCities = Annotated[User, Depends(require_permission("cities.update"))]
ActivateCities = Annotated[User, Depends(require_permission("cities.activate"))]
DeleteCities = Annotated[User, Depends(require_permission("cities.delete"))]
ArchiveEvents = Annotated[User, Depends(require_permission("events.archive"))]
DeleteEvents = Annotated[User, Depends(require_permission("events.delete"))]
ArchiveSites = Annotated[User, Depends(require_permission("sites.archive"))]

PER_PAGE = 20


def _format_errors(exc: ValidationError) -> dict[str, str]:
    result: dict[str, str] = {}
    for err in exc.errors():
        field = ".".join(str(p) for p in err["loc"])
        result[field] = err["msg"]
    return result


def _parse_float(value: str) -> float | None:
    value = value.strip()
    return float(value) if value else None


def _parse_boundary(value: str) -> dict | None:
    value = value.strip()
    return json.loads(value) if value else None


def _build_city_data(
    *,
    name: str,
    slug: str,
    state_or_region: str,
    country: str,
    timezone: str,
    default_latitude: str,
    default_longitude: str,
    boundary_config: str,
    is_active: bool,
    schema_cls: type[CityCreate] | type[CityUpdate],
):
    return schema_cls(
        name=name,
        slug=slug,
        state_or_region=state_or_region or None,
        country=country or None,
        timezone=timezone,
        default_latitude=_parse_float(default_latitude),
        default_longitude=_parse_float(default_longitude),
        boundary_config=_parse_boundary(boundary_config),
        is_active=is_active,
    )


# --- List / search -----------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
def list_cities_view(
    request: Request,
    current_user: ViewCities,
    db: DbSession,
    q: str | None = None,
    status: str = "all",
    page: int = 1,
):
    cities, total = search_cities(db, query=q, status=status, page=page, per_page=PER_PAGE)
    base_url = f"/admin/cities?status={status}"
    if q:
        base_url += f"&q={q}"
    return render(
        request,
        "admin/cities/list.html",
        {
            "current_user": current_user,
            "cities": cities,
            "total": total,
            "page": page,
            "per_page": PER_PAGE,
            "q": q or "",
            "status": status,
            "base_url": base_url,
        },
    )


# --- Create ------------------------------------------------------------------------


@router.get("/new", response_class=HTMLResponse)
def new_city_form(request: Request, current_user: CreateCities):
    return render(
        request,
        "admin/cities/form.html",
        {
            "current_user": current_user,
            "mode": "create",
            "city": None,
            "form": {"timezone": "UTC", "is_active": True},
            "errors": {},
        },
    )


@router.post("", response_class=HTMLResponse)
def create_city_view(
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    ip_address: ClientIp,
    current_user: CreateCities,
    name: str = Form(...),
    slug: str = Form(...),
    state_or_region: str = Form(""),
    country: str = Form(""),
    timezone: str = Form("UTC"),
    default_latitude: str = Form(""),
    default_longitude: str = Form(""),
    boundary_config: str = Form(""),
    is_active: str | None = Form(None),
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    form_values = {
        "name": name,
        "slug": slug,
        "state_or_region": state_or_region,
        "country": country,
        "timezone": timezone,
        "default_latitude": default_latitude,
        "default_longitude": default_longitude,
        "boundary_config": boundary_config,
        "is_active": is_active is not None,
    }

    try:
        data = _build_city_data(
            name=name,
            slug=slug,
            state_or_region=state_or_region,
            country=country,
            timezone=timezone,
            default_latitude=default_latitude,
            default_longitude=default_longitude,
            boundary_config=boundary_config,
            is_active=is_active is not None,
            schema_cls=CityCreate,
        )
    except ValidationError as exc:
        errors = _format_errors(exc)
        return render(
            request,
            "admin/cities/form.html",
            {
                "current_user": current_user,
                "mode": "create",
                "city": None,
                "form": form_values,
                "errors": errors,
            },
            status_code=422,
        )
    except ValueError:
        return render(
            request,
            "admin/cities/form.html",
            {
                "current_user": current_user,
                "mode": "create",
                "city": None,
                "form": form_values,
                "errors": {"boundary_config": "Must be valid JSON, or left blank"},
            },
            status_code=422,
        )

    if get_city_by_slug(db, data.slug) is not None:
        return render(
            request,
            "admin/cities/form.html",
            {
                "current_user": current_user,
                "mode": "create",
                "city": None,
                "form": form_values,
                "errors": {"slug": "A city with this slug already exists."},
            },
            status_code=409,
        )

    city = create_city(db, data)
    record_audit(
        db,
        actor_id=current_user.id,
        action="city_created",
        entity_type="city",
        entity_id=city.id,
        after=data.model_dump(mode="json"),
        correlation_id=correlation_id,
        ip_address=ip_address,
    )
    response = RedirectResponse(url=f"/admin/cities/{city.id}", status_code=303)
    set_flash(response, f"City '{city.name}' created.")
    return response


# --- Detail / edit -------------------------------------------------------------------


@router.get("/{city_id}", response_class=HTMLResponse)
def city_detail(city_id: int, request: Request, current_user: ViewCities, db: DbSession):
    city = get_city(db, city_id)
    if city is None:
        raise NotFoundError("City not found")

    event_count = db.query(Event).filter(Event.city_id == city.id).count()
    website_count = db.query(Website).filter(Website.city_id == city.id).count()
    recent_events = (
        db.query(Event)
        .filter(Event.city_id == city.id)
        .order_by(Event.created_at.desc())
        .limit(10)
        .all()
    )
    recent_websites = (
        db.query(Website)
        .filter(Website.city_id == city.id)
        .order_by(Website.created_at.desc())
        .limit(10)
        .all()
    )

    return render(
        request,
        "admin/cities/detail.html",
        {
            "current_user": current_user,
            "city": city,
            "event_count": event_count,
            "website_count": website_count,
            "recent_events": recent_events,
            "recent_websites": recent_websites,
            "can_delete": user_has_permission(db, current_user, "cities.delete"),
            "can_update": user_has_permission(db, current_user, "cities.update"),
            "can_activate": user_has_permission(db, current_user, "cities.activate"),
        },
    )


@router.get("/{city_id}/edit", response_class=HTMLResponse)
def edit_city_form(city_id: int, request: Request, current_user: UpdateCities, db: DbSession):
    city = get_city(db, city_id)
    if city is None:
        raise NotFoundError("City not found")

    form_values = {
        "name": city.name,
        "slug": city.slug,
        "state_or_region": city.state_or_region or "",
        "country": city.country or "",
        "timezone": city.timezone,
        "default_latitude": city.default_latitude if city.default_latitude is not None else "",
        "default_longitude": city.default_longitude if city.default_longitude is not None else "",
        "boundary_config": json.dumps(city.boundary_config) if city.boundary_config else "",
        "is_active": city.is_active,
    }
    return render(
        request,
        "admin/cities/form.html",
        {
            "current_user": current_user,
            "mode": "edit",
            "city": city,
            "form": form_values,
            "errors": {},
        },
    )


@router.post("/{city_id}", response_class=HTMLResponse)
def update_city_view(
    city_id: int,
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    ip_address: ClientIp,
    current_user: UpdateCities,
    name: str = Form(...),
    slug: str = Form(...),
    state_or_region: str = Form(""),
    country: str = Form(""),
    timezone: str = Form("UTC"),
    default_latitude: str = Form(""),
    default_longitude: str = Form(""),
    boundary_config: str = Form(""),
    is_active: str | None = Form(None),
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    city = get_city(db, city_id)
    if city is None:
        raise NotFoundError("City not found")

    form_values = {
        "name": name,
        "slug": slug,
        "state_or_region": state_or_region,
        "country": country,
        "timezone": timezone,
        "default_latitude": default_latitude,
        "default_longitude": default_longitude,
        "boundary_config": boundary_config,
        "is_active": is_active is not None,
    }

    try:
        data = _build_city_data(
            name=name,
            slug=slug,
            state_or_region=state_or_region,
            country=country,
            timezone=timezone,
            default_latitude=default_latitude,
            default_longitude=default_longitude,
            boundary_config=boundary_config,
            is_active=is_active is not None,
            schema_cls=CityUpdate,
        )
    except ValidationError as exc:
        errors = _format_errors(exc)
        return render(
            request,
            "admin/cities/form.html",
            {
                "current_user": current_user,
                "mode": "edit",
                "city": city,
                "form": form_values,
                "errors": errors,
            },
            status_code=422,
        )
    except ValueError:
        return render(
            request,
            "admin/cities/form.html",
            {
                "current_user": current_user,
                "mode": "edit",
                "city": city,
                "form": form_values,
                "errors": {"boundary_config": "Must be valid JSON, or left blank"},
            },
            status_code=422,
        )

    existing = get_city_by_slug(db, data.slug)
    if existing is not None and existing.id != city.id:
        return render(
            request,
            "admin/cities/form.html",
            {
                "current_user": current_user,
                "mode": "edit",
                "city": city,
                "form": form_values,
                "errors": {"slug": "A city with this slug already exists."},
            },
            status_code=409,
        )

    before = {
        "name": city.name,
        "slug": city.slug,
        "timezone": city.timezone,
        "is_active": city.is_active,
    }
    update_city(db, city, data)
    record_audit(
        db,
        actor_id=current_user.id,
        action="city_updated",
        entity_type="city",
        entity_id=city.id,
        before=before,
        after=data.model_dump(mode="json"),
        correlation_id=correlation_id,
        ip_address=ip_address,
    )
    response = RedirectResponse(url=f"/admin/cities/{city.id}", status_code=303)
    set_flash(response, f"City '{city.name}' updated.")
    return response


# --- Activate / deactivate ----------------------------------------------------------


@router.post("/{city_id}/activate")
def activate_city_view(
    city_id: int,
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    ip_address: ClientIp,
    current_user: ActivateCities,
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    city = get_city(db, city_id)
    if city is None:
        raise NotFoundError("City not found")

    before = {"is_active": city.is_active}
    city.is_active = True
    db.commit()
    record_audit(
        db,
        actor_id=current_user.id,
        action="city_activated",
        entity_type="city",
        entity_id=city.id,
        before=before,
        after={"is_active": True},
        correlation_id=correlation_id,
        ip_address=ip_address,
    )
    response = RedirectResponse(url=f"/admin/cities/{city.id}", status_code=303)
    set_flash(response, f"City '{city.name}' activated.")
    return response


@router.post("/{city_id}/deactivate")
def deactivate_city_view(
    city_id: int,
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    ip_address: ClientIp,
    current_user: ActivateCities,
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    city = get_city(db, city_id)
    if city is None:
        raise NotFoundError("City not found")

    before = {"is_active": city.is_active}
    city.is_active = False
    db.commit()
    record_audit(
        db,
        actor_id=current_user.id,
        action="city_deactivated",
        entity_type="city",
        entity_id=city.id,
        before=before,
        after={"is_active": False},
        correlation_id=correlation_id,
        ip_address=ip_address,
    )
    response = RedirectResponse(url=f"/admin/cities/{city.id}", status_code=303)
    set_flash(response, f"City '{city.name}' deactivated.")
    return response


# --- Safe deletion workflow ----------------------------------------------------------


@router.get("/{city_id}/delete", response_class=HTMLResponse)
def city_delete_impact(city_id: int, request: Request, current_user: DeleteCities, db: DbSession):
    city = get_city(db, city_id)
    if city is None:
        raise NotFoundError("City not found")

    impact = get_deletion_impact(db, city)
    return render(
        request,
        "admin/cities/delete.html",
        {"current_user": current_user, "city": city, "impact": impact, "error": None},
    )


@router.post("/{city_id}/archive-events")
async def archive_events_view(
    city_id: int,
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    ip_address: ClientIp,
    current_user: ArchiveEvents,
    confirm_slug: str = Form(...),
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    await reject_unexpected_form_fields(request, {"confirm_slug", "csrf_token"})
    city = get_city(db, city_id)
    if city is None:
        raise NotFoundError("City not found")
    if confirm_slug != city.slug:
        raise AppError("City slug confirmation does not match", status_code=400)

    count = archive_city_events(db, city)
    record_audit(
        db,
        actor_id=current_user.id,
        action="bulk_city_events_archived",
        entity_type="city",
        entity_id=city.id,
        after={"archived_count": count},
        correlation_id=correlation_id,
        ip_address=ip_address,
    )
    response = RedirectResponse(url=f"/admin/cities/{city.id}/delete", status_code=303)
    set_flash(response, f"Archived {count} event(s).")
    return response


@router.post("/{city_id}/delete-events")
async def delete_events_view(
    city_id: int,
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    ip_address: ClientIp,
    current_user: DeleteEvents,
    confirm_slug: str = Form(...),
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    await reject_unexpected_form_fields(request, {"confirm_slug", "csrf_token"})
    city = get_city(db, city_id)
    if city is None:
        raise NotFoundError("City not found")
    if confirm_slug != city.slug:
        raise AppError("City slug confirmation does not match", status_code=400)

    count = delete_archived_city_events(db, city)
    record_audit(
        db,
        actor_id=current_user.id,
        action="bulk_city_events_deleted",
        entity_type="city",
        entity_id=city.id,
        before={"deleted_count": count},
        correlation_id=correlation_id,
        ip_address=ip_address,
    )
    response = RedirectResponse(url=f"/admin/cities/{city.id}/delete", status_code=303)
    set_flash(response, f"Permanently deleted {count} archived event(s).")
    return response


@router.post("/{city_id}/archive-websites")
def archive_websites_view(
    city_id: int,
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    ip_address: ClientIp,
    current_user: ArchiveSites,
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    city = get_city(db, city_id)
    if city is None:
        raise NotFoundError("City not found")

    count = archive_city_websites(db, city)
    record_audit(
        db,
        actor_id=current_user.id,
        action="websites_archived",
        entity_type="city",
        entity_id=city.id,
        after={"archived_count": count},
        correlation_id=correlation_id,
        ip_address=ip_address,
    )
    response = RedirectResponse(url=f"/admin/cities/{city.id}/delete", status_code=303)
    set_flash(response, f"Archived {count} website(s).")
    return response


@router.post("/{city_id}/delete", response_class=HTMLResponse)
def delete_city_view(
    city_id: int,
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    ip_address: ClientIp,
    current_user: DeleteCities,
    confirm_slug: str = Form(...),
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    city = get_city(db, city_id)
    if city is None:
        raise NotFoundError("City not found")

    impact = get_deletion_impact(db, city)
    if not impact.can_delete:
        return render(
            request,
            "admin/cities/delete.html",
            {
                "current_user": current_user,
                "city": city,
                "impact": impact,
                "error": "Cannot delete: this city still has unarchived events or websites.",
            },
            status_code=409,
        )

    if confirm_slug.strip() != city.slug:
        return render(
            request,
            "admin/cities/delete.html",
            {
                "current_user": current_user,
                "city": city,
                "impact": impact,
                "error": "Confirmation text did not match the city's slug. Nothing was deleted.",
            },
            status_code=400,
        )

    before = {
        "name": city.name,
        "slug": city.slug,
        "archived_events": impact.archived_events,
        "archived_websites": impact.archived_websites,
    }
    deleted_id = city.id
    deleted_name = city.name
    db.delete(city)
    db.commit()
    record_audit(
        db,
        actor_id=current_user.id,
        action="city_deleted",
        entity_type="city",
        entity_id=deleted_id,
        before=before,
        correlation_id=correlation_id,
        ip_address=ip_address,
    )
    response = RedirectResponse(url="/admin/cities", status_code=303)
    set_flash(response, f"City '{deleted_name}' permanently deleted.")
    return response
