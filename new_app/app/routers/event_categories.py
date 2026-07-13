import re
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.core.csrf import verify_csrf
from app.core.exceptions import AppError, NotFoundError
from app.core.flash import set_flash
from app.core.forms import reject_unexpected_form_fields
from app.core.templating import render
from app.dependencies import ClientIp, CorrelationId, DbSession
from app.models.categorization_rule import CategorizationRule
from app.models.event import Event
from app.models.event_category import EventCategory
from app.models.user import User
from app.services.audit import record_audit
from app.services.rbac import require_permission

router = APIRouter(prefix="/admin/event-categories", tags=["admin-event-categories"])
ManageCategories = Annotated[User, Depends(require_permission("settings.manage"))]


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.strip().casefold()).strip("-")


def _category_or_404(db: DbSession, category_id: int) -> EventCategory:
    category = db.get(EventCategory, category_id)
    if category is None:
        raise NotFoundError("Category not found")
    return category


def _audit_category(
    db: DbSession,
    actor: User,
    action: str,
    category: EventCategory,
    correlation_id: str | None,
    ip_address: str | None,
    *,
    before: dict | None = None,
    after: dict | None = None,
) -> None:
    record_audit(
        db,
        actor_id=actor.id,
        action=action,
        entity_type="event_category",
        entity_id=category.id,
        before=before,
        after=after,
        correlation_id=correlation_id,
        ip_address=ip_address,
    )


@router.get("", response_class=HTMLResponse)
def list_categories(request: Request, current_user: ManageCategories, db: DbSession):
    categories = (
        db.query(EventCategory).order_by(EventCategory.display_order, EventCategory.name).all()
    )
    return render(
        request,
        "admin/categories/list.html",
        {"current_user": current_user, "categories": categories},
    )


@router.post("")
async def create_category(
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    ip_address: ClientIp,
    current_user: ManageCategories,
    name: str = Form(...),
    description: str = Form(""),
    display_order: int = Form(0),
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    await reject_unexpected_form_fields(
        request, {"name", "description", "display_order", "csrf_token"}
    )
    name = name.strip()
    slug = _slugify(name)
    if not name or not slug:
        raise AppError("Category name is required", status_code=422)
    if (
        db.query(EventCategory)
        .filter((EventCategory.name == name) | (EventCategory.slug == slug))
        .first()
    ):
        raise AppError("A category with this name or slug already exists", status_code=409)
    category = EventCategory(
        name=name,
        slug=slug,
        description=description.strip() or None,
        display_order=display_order,
        is_active=True,
    )
    db.add(category)
    db.commit()
    db.refresh(category)
    _audit_category(
        db,
        current_user,
        "category_created",
        category,
        correlation_id,
        ip_address,
        after={"name": category.name, "slug": category.slug},
    )
    response = RedirectResponse("/admin/event-categories", status_code=303)
    set_flash(response, "Category created.")
    return response


@router.post("/{category_id}")
async def update_category(
    category_id: int,
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    ip_address: ClientIp,
    current_user: ManageCategories,
    name: str = Form(...),
    description: str = Form(""),
    display_order: int = Form(0),
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    await reject_unexpected_form_fields(
        request, {"name", "description", "display_order", "csrf_token"}
    )
    category = _category_or_404(db, category_id)
    name = name.strip()
    slug = _slugify(name)
    if not name or not slug:
        raise AppError("Category name is required", status_code=422)
    conflict = (
        db.query(EventCategory)
        .filter(
            EventCategory.id != category.id,
            (EventCategory.name == name) | (EventCategory.slug == slug),
        )
        .first()
    )
    if conflict:
        raise AppError("A category with this name or slug already exists", status_code=409)
    before = {
        "name": category.name,
        "slug": category.slug,
        "description": category.description,
        "display_order": category.display_order,
    }
    category.name = name
    category.slug = slug
    category.description = description.strip() or None
    category.display_order = display_order
    db.commit()
    _audit_category(
        db,
        current_user,
        "category_updated",
        category,
        correlation_id,
        ip_address,
        before=before,
        after={
            "name": category.name,
            "slug": category.slug,
            "description": category.description,
            "display_order": category.display_order,
        },
    )
    return RedirectResponse("/admin/event-categories", status_code=303)


@router.post("/{category_id}/status")
async def update_category_status(
    category_id: int,
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    ip_address: ClientIp,
    current_user: ManageCategories,
    action: str = Form(...),
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    await reject_unexpected_form_fields(request, {"action", "csrf_token"})
    if action not in {"activate", "deactivate"}:
        raise AppError("Invalid category action", status_code=422)
    category = _category_or_404(db, category_id)
    before = {"is_active": category.is_active}
    category.is_active = action == "activate"
    db.commit()
    _audit_category(
        db,
        current_user,
        f"category_{action}d",
        category,
        correlation_id,
        ip_address,
        before=before,
        after={"is_active": category.is_active},
    )
    return RedirectResponse("/admin/event-categories", status_code=303)


@router.post("/{category_id}/delete")
async def delete_category(
    category_id: int,
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    ip_address: ClientIp,
    current_user: ManageCategories,
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    await reject_unexpected_form_fields(request, {"csrf_token"})
    category = _category_or_404(db, category_id)
    event_count = (
        db.query(Event)
        .filter((Event.category_id == category.id) | (Event.category_override_id == category.id))
        .count()
    )
    rule_count = (
        db.query(CategorizationRule).filter(CategorizationRule.category_id == category.id).count()
    )
    if event_count or rule_count:
        raise AppError("Category is still referenced by events or rules", status_code=409)
    snapshot = {"name": category.name, "slug": category.slug}
    category_id_for_audit = category.id
    db.delete(category)
    db.commit()
    record_audit(
        db,
        actor_id=current_user.id,
        action="category_deleted",
        entity_type="event_category",
        entity_id=category_id_for_audit,
        before=snapshot,
        correlation_id=correlation_id,
        ip_address=ip_address,
    )
    return RedirectResponse("/admin/event-categories", status_code=303)
