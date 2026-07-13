from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.core.csrf import verify_csrf
from app.core.exceptions import AppError, NotFoundError
from app.core.forms import reject_unexpected_form_fields
from app.core.templating import render
from app.dependencies import ClientIp, CorrelationId, DbSession
from app.models.categorization_rule import CategorizationRule
from app.models.event_category import EventCategory
from app.models.user import User
from app.models.website import Website
from app.services.audit import record_audit
from app.services.categorization import RULE_TYPES, validate_rule_pattern
from app.services.rbac import require_permission

router = APIRouter(prefix="/admin/categorization-rules", tags=["admin-categorization-rules"])
ManageRules = Annotated[User, Depends(require_permission("settings.manage"))]


def _rule_or_404(db: DbSession, rule_id: int) -> CategorizationRule:
    rule = db.get(CategorizationRule, rule_id)
    if rule is None:
        raise NotFoundError("Categorization rule not found")
    return rule


def _optional_int(value: str) -> int | None:
    if not value.strip():
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise AppError("Website ID must be an integer", status_code=422) from exc


def _validated_fields(
    db: DbSession,
    *,
    name: str,
    rule_type: str,
    category_id: int,
    priority: int,
    website_id: str,
    source_category_value: str,
    pattern: str,
    is_regex: bool,
    case_sensitive: bool,
) -> dict:
    if rule_type not in RULE_TYPES:
        raise AppError("Invalid rule type", status_code=422)
    category = db.get(EventCategory, category_id)
    if category is None or not category.is_active:
        raise AppError("An active category is required", status_code=422)
    parsed_website_id = _optional_int(website_id)
    if parsed_website_id is not None and db.get(Website, parsed_website_id) is None:
        raise AppError("Website not found", status_code=422)
    if rule_type == "website_mapping" and parsed_website_id is None:
        raise AppError("Website mapping rules require a website", status_code=422)
    source_value = source_category_value.strip() or None
    if rule_type in {"source_mapping", "administrator_mapping"} and not source_value:
        raise AppError("This rule type requires a source category value", status_code=422)
    try:
        cleaned_pattern = validate_rule_pattern(pattern, is_regex=is_regex)
    except ValueError as exc:
        raise AppError(str(exc), status_code=422) from exc
    if rule_type in {"venue", "keyword"} and not cleaned_pattern:
        raise AppError("This rule type requires a pattern", status_code=422)
    return {
        "name": name.strip(),
        "rule_type": rule_type,
        "category_id": category.id,
        "priority": priority,
        "website_id": parsed_website_id,
        "source_category_value": source_value,
        "pattern": cleaned_pattern,
        "is_regex": is_regex,
        "case_sensitive": case_sensitive,
    }


@router.get("", response_class=HTMLResponse)
def list_rules(request: Request, current_user: ManageRules, db: DbSession):
    return render(
        request,
        "admin/rules/list.html",
        {
            "current_user": current_user,
            "rules": db.query(CategorizationRule).order_by(CategorizationRule.name).all(),
            "categories": (
                db.query(EventCategory)
                .filter(EventCategory.is_active.is_(True))
                .order_by(EventCategory.display_order)
                .all()
            ),
            "websites": db.query(Website).order_by(Website.name).all(),
            "rule_types": RULE_TYPES,
        },
    )


@router.post("")
async def create_rule(
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    ip_address: ClientIp,
    current_user: ManageRules,
    name: str = Form(...),
    rule_type: str = Form(...),
    category_id: int = Form(...),
    priority: int = Form(0),
    website_id: str = Form(""),
    source_category_value: str = Form(""),
    pattern: str = Form(""),
    is_regex: str | None = Form(None),
    case_sensitive: str | None = Form(None),
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    await reject_unexpected_form_fields(
        request,
        {
            "name",
            "rule_type",
            "category_id",
            "priority",
            "website_id",
            "source_category_value",
            "pattern",
            "is_regex",
            "case_sensitive",
            "csrf_token",
        },
    )
    fields = _validated_fields(
        db,
        name=name,
        rule_type=rule_type,
        category_id=category_id,
        priority=priority,
        website_id=website_id,
        source_category_value=source_category_value,
        pattern=pattern,
        is_regex=is_regex is not None,
        case_sensitive=case_sensitive is not None,
    )
    if not fields["name"]:
        raise AppError("Rule name is required", status_code=422)
    if db.query(CategorizationRule).filter(CategorizationRule.name == fields["name"]).first():
        raise AppError("A rule with this name already exists", status_code=409)
    rule = CategorizationRule(**fields, is_active=True)
    db.add(rule)
    db.commit()
    db.refresh(rule)
    record_audit(
        db,
        actor_id=current_user.id,
        action="categorization_rule_created",
        entity_type="categorization_rule",
        entity_id=rule.id,
        after=fields,
        correlation_id=correlation_id,
        ip_address=ip_address,
    )
    return RedirectResponse("/admin/categorization-rules", status_code=303)


@router.post("/{rule_id}/status")
async def update_rule_status(
    rule_id: int,
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    ip_address: ClientIp,
    current_user: ManageRules,
    action: str = Form(...),
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    await reject_unexpected_form_fields(request, {"action", "csrf_token"})
    if action not in {"activate", "deactivate"}:
        raise AppError("Invalid rule action", status_code=422)
    rule = _rule_or_404(db, rule_id)
    before = {"is_active": rule.is_active}
    rule.is_active = action == "activate"
    db.commit()
    record_audit(
        db,
        actor_id=current_user.id,
        action=f"categorization_rule_{action}d",
        entity_type="categorization_rule",
        entity_id=rule.id,
        before=before,
        after={"is_active": rule.is_active},
        correlation_id=correlation_id,
        ip_address=ip_address,
    )
    return RedirectResponse("/admin/categorization-rules", status_code=303)


@router.post("/{rule_id}")
async def update_rule(
    rule_id: int,
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    ip_address: ClientIp,
    current_user: ManageRules,
    name: str = Form(...),
    rule_type: str = Form(...),
    category_id: int = Form(...),
    priority: int = Form(0),
    website_id: str = Form(""),
    source_category_value: str = Form(""),
    pattern: str = Form(""),
    is_regex: str | None = Form(None),
    case_sensitive: str | None = Form(None),
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    await reject_unexpected_form_fields(
        request,
        {
            "name",
            "rule_type",
            "category_id",
            "priority",
            "website_id",
            "source_category_value",
            "pattern",
            "is_regex",
            "case_sensitive",
            "csrf_token",
        },
    )
    rule = _rule_or_404(db, rule_id)
    fields = _validated_fields(
        db,
        name=name,
        rule_type=rule_type,
        category_id=category_id,
        priority=priority,
        website_id=website_id,
        source_category_value=source_category_value,
        pattern=pattern,
        is_regex=is_regex is not None,
        case_sensitive=case_sensitive is not None,
    )
    if not fields["name"]:
        raise AppError("Rule name is required", status_code=422)
    conflict = (
        db.query(CategorizationRule)
        .filter(CategorizationRule.id != rule.id, CategorizationRule.name == fields["name"])
        .first()
    )
    if conflict:
        raise AppError("A rule with this name already exists", status_code=409)
    before = {
        "name": rule.name,
        "rule_type": rule.rule_type,
        "category_id": rule.category_id,
        "priority": rule.priority,
        "website_id": rule.website_id,
        "source_category_value": rule.source_category_value,
        "pattern": rule.pattern,
        "is_regex": rule.is_regex,
        "case_sensitive": rule.case_sensitive,
    }
    for field, value in fields.items():
        setattr(rule, field, value)
    db.commit()
    record_audit(
        db,
        actor_id=current_user.id,
        action="categorization_rule_updated",
        entity_type="categorization_rule",
        entity_id=rule.id,
        before=before,
        after=fields,
        correlation_id=correlation_id,
        ip_address=ip_address,
    )
    return RedirectResponse("/admin/categorization-rules", status_code=303)
