from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import or_

from app.core.csrf import verify_csrf
from app.core.exceptions import AppError, NotFoundError
from app.core.flash import set_flash
from app.core.forms import reject_unexpected_form_fields
from app.core.templating import render
from app.dependencies import ClientIp, CorrelationId, DbSession
from app.models.city import City
from app.models.event import Event
from app.models.event_category import EventCategory
from app.models.user import User
from app.models.website import Website
from app.repositories.event_provenance import get_latest_provenance_for_event
from app.services.audit import record_audit
from app.services.categorization import apply_categorization, categorize_event
from app.services.rbac import get_effective_permissions, require_permission, user_has_permission

router = APIRouter(prefix="/admin/events", tags=["admin-events"])

ViewEvents = Annotated[User, Depends(require_permission("events.view"))]
PER_PAGE = 20


def _event_or_404(db: DbSession, event_id: int) -> Event:
    event = db.get(Event, event_id)
    if event is None:
        raise NotFoundError("Event not found")
    return event


def _require(db: DbSession, user: User, code: str) -> None:
    if not user_has_permission(db, user, code):
        raise AppError("Forbidden: missing permission", status_code=403)


def _audit(
    db: DbSession,
    user: User,
    action: str,
    event: Event,
    correlation_id: str | None,
    ip_address: str | None,
    *,
    before: dict | None = None,
    after: dict | None = None,
) -> None:
    record_audit(
        db,
        actor_id=user.id,
        action=action,
        entity_type="event",
        entity_id=event.id,
        before=before,
        after=after,
        correlation_id=correlation_id,
        ip_address=ip_address,
    )


@router.get("", response_class=HTMLResponse)
def list_events(
    request: Request,
    current_user: ViewEvents,
    db: DbSession,
    q: str = "",
    city_id: int | None = None,
    website_id: int | None = None,
    category_id: int | None = None,
    active: str = "all",
    archived: str = "no",
    review_status: str = "all",
    duplicate_status: str = "all",
    page: int = Query(1, ge=1),
):
    query = db.query(Event)
    if q.strip():
        term = f"%{q.strip()}%"
        query = query.filter(
            or_(Event.title.ilike(term), Event.venue.ilike(term), Event.source.ilike(term))
        )
    if city_id is not None:
        query = query.filter(Event.city_id == city_id)
    if website_id is not None:
        query = query.filter(Event.website_id == website_id)
    if category_id is not None:
        query = query.filter(
            or_(Event.category_id == category_id, Event.category_override_id == category_id)
        )
    if active in {"yes", "no"}:
        query = query.filter(Event.is_active.is_(active == "yes"))
    if archived in {"yes", "no"}:
        query = query.filter(
            Event.archived_at.isnot(None) if archived == "yes" else Event.archived_at.is_(None)
        )
    if review_status != "all":
        query = query.filter(Event.review_status == review_status)
    if duplicate_status != "all":
        query = query.filter(Event.duplicate_status == duplicate_status)

    total = query.count()
    events = (
        query.order_by(Event.start_date.desc(), Event.id.desc())
        .offset((page - 1) * PER_PAGE)
        .limit(PER_PAGE)
        .all()
    )
    return render(
        request,
        "admin/events/list.html",
        {
            "current_user": current_user,
            "events": events,
            "cities": db.query(City).order_by(City.name).all(),
            "websites": db.query(Website).order_by(Website.name).all(),
            "categories": db.query(EventCategory).order_by(EventCategory.display_order).all(),
            "filters": {
                "q": q,
                "city_id": city_id,
                "website_id": website_id,
                "category_id": category_id,
                "active": active,
                "archived": archived,
                "review_status": review_status,
                "duplicate_status": duplicate_status,
            },
            "page": page,
            "total": total,
            "has_next": page * PER_PAGE < total,
        },
    )


@router.get("/{event_id}", response_class=HTMLResponse)
def event_detail(event_id: int, request: Request, current_user: ViewEvents, db: DbSession):
    event = _event_or_404(db, event_id)
    permissions = get_effective_permissions(db, current_user)
    return render(
        request,
        "admin/events/detail.html",
        {
            "current_user": current_user,
            "event": event,
            "permissions": permissions,
            "categorization_result": categorize_event(db, event),
            "categories": (
                db.query(EventCategory)
                .filter(EventCategory.is_active.is_(True))
                .order_by(EventCategory.display_order, EventCategory.name)
                .all()
            ),
            "latest_provenance": (
                get_latest_provenance_for_event(db, event.id)
                if "events.view_provenance" in permissions
                else None
            ),
        },
    )


@router.post("/{event_id}/lifecycle")
async def update_lifecycle(
    event_id: int,
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    ip_address: ClientIp,
    current_user: ViewEvents,
    action: str = Form(...),
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    await reject_unexpected_form_fields(request, {"action", "csrf_token"})
    event = _event_or_404(db, event_id)
    before = {"is_active": event.is_active, "archived_at": str(event.archived_at)}

    if action == "activate":
        _require(db, current_user, "events.activate")
        if event.archived_at is not None or event.is_active:
            raise AppError("Only inactive, unarchived events can be activated", status_code=409)
        event.is_active = True
        audit_action = "event_activated"
    elif action == "deactivate":
        _require(db, current_user, "events.activate")
        if not event.is_active:
            raise AppError("Event is already inactive", status_code=409)
        event.is_active = False
        audit_action = "event_deactivated"
    elif action == "archive":
        _require(db, current_user, "events.archive")
        if event.archived_at is not None:
            raise AppError("Event is already archived", status_code=409)
        event.is_active = False
        event.archived_at = datetime.now(UTC)
        audit_action = "event_archived"
    elif action == "restore":
        _require(db, current_user, "events.archive")
        if event.archived_at is None:
            raise AppError("Only archived events can be restored", status_code=409)
        event.archived_at = None
        event.is_active = False
        audit_action = "event_restored"
    else:
        raise AppError("Invalid lifecycle action", status_code=400)

    db.commit()
    _audit(
        db,
        current_user,
        audit_action,
        event,
        correlation_id,
        ip_address,
        before=before,
        after={"is_active": event.is_active, "archived_at": str(event.archived_at)},
    )
    response = RedirectResponse(f"/admin/events/{event.id}", status_code=303)
    set_flash(response, "Event lifecycle updated.")
    return response


@router.post("/{event_id}/delete")
async def permanently_delete_event(
    event_id: int,
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    ip_address: ClientIp,
    current_user: ViewEvents,
    confirm_title: str = Form(...),
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    await reject_unexpected_form_fields(request, {"confirm_title", "csrf_token"})
    _require(db, current_user, "events.delete")
    event = _event_or_404(db, event_id)
    if event.archived_at is None:
        raise AppError("Event must be archived before permanent deletion", status_code=409)
    if confirm_title != event.title:
        raise AppError("Event title confirmation does not match", status_code=400)
    snapshot = {"title": event.title, "city_id": event.city_id, "website_id": event.website_id}
    deleted_id = event.id
    db.delete(event)
    db.commit()
    record_audit(
        db,
        actor_id=current_user.id,
        action="event_permanently_deleted",
        entity_type="event",
        entity_id=deleted_id,
        before=snapshot,
        correlation_id=correlation_id,
        ip_address=ip_address,
    )
    return RedirectResponse("/admin/events", status_code=303)


@router.post("/{event_id}/review-status")
async def update_review_status(
    event_id: int,
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    ip_address: ClientIp,
    current_user: ViewEvents,
    review_status: str = Form(...),
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    await reject_unexpected_form_fields(request, {"review_status", "csrf_token"})
    _require(db, current_user, "events.review")
    if review_status not in {"needs_review", "reviewed"}:
        raise AppError("Invalid review status", status_code=422)
    event = _event_or_404(db, event_id)
    before = {"review_status": event.review_status}
    event.review_status = review_status
    db.commit()
    _audit(
        db,
        current_user,
        "event_review_status_changed",
        event,
        correlation_id,
        ip_address,
        before=before,
        after={"review_status": review_status},
    )
    return RedirectResponse(f"/admin/events/{event.id}", status_code=303)


@router.post("/{event_id}/category-override")
async def override_category(
    event_id: int,
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    ip_address: ClientIp,
    current_user: ViewEvents,
    category_id: int = Form(...),
    reason: str = Form(""),
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    await reject_unexpected_form_fields(request, {"category_id", "reason", "csrf_token"})
    _require(db, current_user, "events.override_category")
    category = db.get(EventCategory, category_id)
    if category is None or not category.is_active:
        raise AppError("An active category is required", status_code=422)
    event = _event_or_404(db, event_id)
    before = {
        "override_category_id": event.category_override_id,
        "effective_category_id": event.effective_category.id if event.effective_category else None,
    }
    event.category_override_id = category.id
    event.category_overridden_by_user_id = current_user.id
    event.category_overridden_at = datetime.now(UTC)
    event.category_override_reason = reason.strip() or None
    db.commit()
    _audit(
        db,
        current_user,
        "event_category_overridden",
        event,
        correlation_id,
        ip_address,
        before=before,
        after={"override_category_id": category.id, "resulting_category_id": category.id},
    )
    return RedirectResponse(f"/admin/events/{event.id}", status_code=303)


@router.post("/{event_id}/category-override/clear")
async def clear_category_override(
    event_id: int,
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    ip_address: ClientIp,
    current_user: ViewEvents,
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    await reject_unexpected_form_fields(request, {"csrf_token"})
    _require(db, current_user, "events.override_category")
    event = _event_or_404(db, event_id)
    before = {"override_category_id": event.category_override_id}
    event.category_override_id = None
    event.category_overridden_by_user_id = None
    event.category_overridden_at = None
    event.category_override_reason = None
    db.commit()
    _audit(
        db,
        current_user,
        "event_category_override_cleared",
        event,
        correlation_id,
        ip_address,
        before=before,
        after={"resulting_category_id": event.category_id},
    )
    return RedirectResponse(f"/admin/events/{event.id}", status_code=303)


def _coordinate(value: str, *, latitude: bool) -> float | None:
    if not value.strip():
        return None
    try:
        result = float(value)
    except ValueError as exc:
        raise AppError("Coordinates must be numeric", status_code=422) from exc
    minimum, maximum = (-90, 90) if latitude else (-180, 180)
    if not minimum <= result <= maximum:
        raise AppError(f"Coordinate must be between {minimum} and {maximum}", status_code=422)
    return result


@router.post("/{event_id}/location-correction")
async def correct_location(
    event_id: int,
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    ip_address: ClientIp,
    current_user: ViewEvents,
    venue: str = Form(""),
    address: str = Form(""),
    latitude: str = Form(""),
    longitude: str = Form(""),
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    await reject_unexpected_form_fields(
        request, {"venue", "address", "latitude", "longitude", "csrf_token"}
    )
    _require(db, current_user, "events.correct_location")
    event = _event_or_404(db, event_id)
    before = {
        "venue": event.corrected_venue,
        "address": event.corrected_address,
        "latitude": event.corrected_latitude,
        "longitude": event.corrected_longitude,
    }
    event.corrected_venue = venue.strip() or None
    event.corrected_address = address.strip() or None
    event.corrected_latitude = _coordinate(latitude, latitude=True)
    event.corrected_longitude = _coordinate(longitude, latitude=False)
    event.location_corrected_by_user_id = current_user.id
    event.location_corrected_at = datetime.now(UTC)
    db.commit()
    after = {
        "venue": event.corrected_venue,
        "address": event.corrected_address,
        "latitude": event.corrected_latitude,
        "longitude": event.corrected_longitude,
    }
    _audit(
        db,
        current_user,
        "event_location_corrected",
        event,
        correlation_id,
        ip_address,
        before=before,
        after=after,
    )
    return RedirectResponse(f"/admin/events/{event.id}", status_code=303)


@router.post("/{event_id}/location-correction/clear")
async def clear_location_correction(
    event_id: int,
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    ip_address: ClientIp,
    current_user: ViewEvents,
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    await reject_unexpected_form_fields(request, {"csrf_token"})
    _require(db, current_user, "events.correct_location")
    event = _event_or_404(db, event_id)
    before = {
        "venue": event.corrected_venue,
        "address": event.corrected_address,
        "latitude": event.corrected_latitude,
        "longitude": event.corrected_longitude,
    }
    event.corrected_venue = None
    event.corrected_address = None
    event.corrected_latitude = None
    event.corrected_longitude = None
    event.location_corrected_by_user_id = None
    event.location_corrected_at = None
    db.commit()
    _audit(
        db,
        current_user,
        "event_location_correction_cleared",
        event,
        correlation_id,
        ip_address,
        before=before,
        after={"venue": None, "address": None, "latitude": None, "longitude": None},
    )
    return RedirectResponse(f"/admin/events/{event.id}", status_code=303)


@router.post("/{event_id}/duplicate-status")
async def update_duplicate_status(
    event_id: int,
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    ip_address: ClientIp,
    current_user: ViewEvents,
    duplicate_status: str = Form(...),
    preferred_event_id: int | None = Form(None),
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    await reject_unexpected_form_fields(
        request, {"duplicate_status", "preferred_event_id", "csrf_token"}
    )
    _require(db, current_user, "events.resolve_duplicates")
    allowed = {"not_reviewed", "possible_duplicate", "confirmed_duplicate", "not_duplicate"}
    if duplicate_status not in allowed:
        raise AppError("Invalid duplicate status", status_code=422)
    event = _event_or_404(db, event_id)
    if preferred_event_id is not None:
        preferred = _event_or_404(db, preferred_event_id)
        if preferred.id == event.id:
            raise AppError("Preferred event must be a different record", status_code=422)
    before = {
        "duplicate_status": event.duplicate_status,
        "preferred_event_id": event.duplicate_preferred_event_id,
    }
    event.duplicate_status = duplicate_status
    event.duplicate_preferred_event_id = (
        preferred_event_id if duplicate_status == "confirmed_duplicate" else None
    )
    db.commit()
    _audit(
        db,
        current_user,
        "duplicate_marked"
        if duplicate_status == "possible_duplicate"
        else "duplicate_resolution_changed",
        event,
        correlation_id,
        ip_address,
        before=before,
        after={
            "duplicate_status": duplicate_status,
            "preferred_event_id": event.duplicate_preferred_event_id,
        },
    )
    return RedirectResponse(f"/admin/events/{event.id}", status_code=303)


@router.post("/{event_id}/categorize")
async def recategorize_event(
    event_id: int,
    request: Request,
    db: DbSession,
    current_user: ViewEvents,
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    await reject_unexpected_form_fields(request, {"csrf_token"})
    _require(db, current_user, "events.review")
    event = _event_or_404(db, event_id)
    apply_categorization(db, event)
    return RedirectResponse(f"/admin/events/{event.id}", status_code=303)
