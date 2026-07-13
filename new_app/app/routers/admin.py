from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.core.csrf import verify_csrf
from app.core.exceptions import AppError, NotFoundError
from app.core.permissions import SUPER_ADMINISTRATOR
from app.core.templating import render
from app.dependencies import ClientIp, CorrelationId, CurrentUser, DbSession
from app.models.audit_log import AuditLog
from app.models.city import City
from app.models.event import Event
from app.models.permission import Permission
from app.models.role import Role
from app.models.role_permission import RolePermission
from app.models.user import User
from app.models.user_role import UserRole
from app.models.website import Website
from app.services.audit import record_audit
from app.services.rbac import (
    assert_not_last_super_admin,
    can_access_admin,
    can_assign_role,
    count_active_super_admins,
    get_effective_permissions,
    require_permission,
    user_has_permission,
)

router = APIRouter(prefix="/admin", tags=["admin"])

ViewUsers = Annotated[User, Depends(require_permission("users.view"))]
UpdateUsers = Annotated[User, Depends(require_permission("users.update"))]
ManageRoles = Annotated[User, Depends(require_permission("roles.manage"))]


# --- Dashboard (any logged-in user) ---------------------------------------------


@router.get("", response_class=HTMLResponse)
def dashboard(request: Request, current_user: CurrentUser, db: DbSession):
    if not can_access_admin(db, current_user):
        raise AppError("Forbidden: no admin access", status_code=403)

    permissions = sorted(get_effective_permissions(db, current_user))

    metrics = {
        "active_cities": db.query(City).filter(City.is_active.is_(True)).count(),
        "inactive_cities": db.query(City).filter(City.is_active.is_(False)).count(),
        "websites": db.query(Website).count(),
        "events": db.query(Event).count(),
    }

    recent_audit = None
    if user_has_permission(db, current_user, "roles.manage"):
        recent_audit = db.query(AuditLog).order_by(AuditLog.created_at.desc()).limit(10).all()

    return render(
        request,
        "admin/dashboard.html",
        {
            "current_user": current_user,
            "permissions": permissions,
            "metrics": metrics,
            "recent_audit": recent_audit,
        },
    )


# --- Users -----------------------------------------------------------------------


@router.get("/users", response_class=HTMLResponse)
def list_users(request: Request, current_user: ViewUsers, db: DbSession):
    users = db.query(User).order_by(User.email).all()
    return render(request, "admin/users.html", {"current_user": current_user, "users": users})


@router.post("/users/{user_id}/activate")
def activate_user(
    user_id: int,
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    ip_address: ClientIp,
    current_user: UpdateUsers,
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    target = db.get(User, user_id)
    if target is None:
        raise NotFoundError("User not found")

    before = {"is_active": target.is_active}
    target.is_active = True
    db.commit()
    record_audit(
        db,
        actor_id=current_user.id,
        action="user_activated",
        entity_type="user",
        entity_id=target.id,
        before=before,
        after={"is_active": True},
        correlation_id=correlation_id,
        ip_address=ip_address,
    )
    return RedirectResponse(url="/admin/users", status_code=303)


@router.post("/users/{user_id}/deactivate")
def deactivate_user(
    user_id: int,
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    ip_address: ClientIp,
    current_user: UpdateUsers,
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    target = db.get(User, user_id)
    if target is None:
        raise NotFoundError("User not found")

    assert_not_last_super_admin(db, target)

    before = {"is_active": target.is_active}
    target.is_active = False
    db.commit()
    record_audit(
        db,
        actor_id=current_user.id,
        action="user_deactivated",
        entity_type="user",
        entity_id=target.id,
        before=before,
        after={"is_active": False},
        correlation_id=correlation_id,
        ip_address=ip_address,
    )
    return RedirectResponse(url="/admin/users", status_code=303)


@router.get("/users/{user_id}/effective-permissions", response_class=HTMLResponse)
def user_effective_permissions(
    user_id: int, request: Request, current_user: ManageRoles, db: DbSession
):
    target = db.get(User, user_id)
    if target is None:
        raise NotFoundError("User not found")

    permissions = sorted(get_effective_permissions(db, target))
    roles = [ur.role for ur in target.user_roles]
    all_roles = db.query(Role).filter(Role.is_active.is_(True)).order_by(Role.name).all()
    return render(
        request,
        "admin/user_permissions.html",
        {
            "current_user": current_user,
            "target": target,
            "permissions": permissions,
            "roles": roles,
            "all_roles": all_roles,
        },
    )


@router.post("/users/{user_id}/roles")
def update_user_roles(
    user_id: int,
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    ip_address: ClientIp,
    current_user: ManageRoles,
    role_id: int = Form(...),
    action: str = Form(...),
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    target = db.get(User, user_id)
    if target is None:
        raise NotFoundError("User not found")
    role = db.get(Role, role_id)
    if role is None:
        raise NotFoundError("Role not found")

    existing = (
        db.query(UserRole)
        .filter(UserRole.user_id == target.id, UserRole.role_id == role.id)
        .first()
    )

    if action == "assign":
        if not role.is_active:
            raise AppError("Cannot assign a deactivated role.", status_code=409)
        if not can_assign_role(db, current_user, role):
            raise AppError(
                "Only a Super Administrator may assign Administrator or Super Administrator.",
                status_code=403,
            )
        if existing is None:
            db.add(UserRole(user_id=target.id, role_id=role.id))
            db.commit()
            record_audit(
                db,
                actor_id=current_user.id,
                action="role_assigned",
                entity_type="user",
                entity_id=target.id,
                after={"role": role.name},
                correlation_id=correlation_id,
                ip_address=ip_address,
            )
    elif action == "revoke":
        if existing is not None:
            # Revoking Administrator/Super Administrator is exactly as sensitive
            # as granting it — an ordinary roles.manage holder must not be able
            # to strip another admin's elevated access either.
            if not can_assign_role(db, current_user, role):
                raise AppError(
                    "Only a Super Administrator may revoke Administrator or Super Administrator.",
                    status_code=403,
                )
            # Only the removal of the Super Administrator assignment itself needs
            # the last-active-admin guard — revoking an unrelated role from a
            # super admin is fine.
            if role.name == SUPER_ADMINISTRATOR:
                assert_not_last_super_admin(db, target)
            db.delete(existing)
            db.commit()
            record_audit(
                db,
                actor_id=current_user.id,
                action="role_unassigned",
                entity_type="user",
                entity_id=target.id,
                before={"role": role.name},
                correlation_id=correlation_id,
                ip_address=ip_address,
            )
    else:
        raise AppError("Invalid action", status_code=400)

    return RedirectResponse(url=f"/admin/users/{target.id}/effective-permissions", status_code=303)


# --- Roles & permissions ---------------------------------------------------------


@router.get("/roles", response_class=HTMLResponse)
def list_roles(request: Request, current_user: ManageRoles, db: DbSession):
    roles = db.query(Role).order_by(Role.name).all()
    return render(request, "admin/roles.html", {"current_user": current_user, "roles": roles})


@router.post("/roles")
def create_role(
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    ip_address: ClientIp,
    current_user: ManageRoles,
    name: str = Form(...),
    description: str = Form(""),
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    if db.query(Role).filter(Role.name == name).first() is not None:
        raise AppError("A role with this name already exists", status_code=400)

    role = Role(name=name, description=description or None)
    db.add(role)
    db.commit()
    db.refresh(role)

    record_audit(
        db,
        actor_id=current_user.id,
        action="role_created",
        entity_type="role",
        entity_id=role.id,
        after={"name": role.name, "description": role.description},
        correlation_id=correlation_id,
        ip_address=ip_address,
    )
    return RedirectResponse(url=f"/admin/roles/{role.id}", status_code=303)


@router.get("/roles/{role_id}", response_class=HTMLResponse)
def edit_role_form(role_id: int, request: Request, current_user: ManageRoles, db: DbSession):
    role = db.get(Role, role_id)
    if role is None:
        raise NotFoundError("Role not found")

    all_permissions = db.query(Permission).order_by(Permission.code).all()
    assigned_codes = {rp.permission.code for rp in role.role_permissions}
    return render(
        request,
        "admin/role_edit.html",
        {
            "current_user": current_user,
            "role": role,
            "all_permissions": all_permissions,
            "assigned_codes": assigned_codes,
        },
    )


@router.post("/roles/{role_id}")
def update_role(
    role_id: int,
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    ip_address: ClientIp,
    current_user: ManageRoles,
    name: str = Form(...),
    description: str = Form(""),
    is_active: str | None = Form(None),
    permission_codes: list[str] | None = Form(None),  # noqa: B008
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    role = db.get(Role, role_id)
    if role is None:
        raise NotFoundError("Role not found")

    before = {
        "name": role.name,
        "description": role.description,
        "is_active": role.is_active,
        "permissions": sorted(rp.permission.code for rp in role.role_permissions),
    }

    new_active = is_active is not None
    if (
        role.name == SUPER_ADMINISTRATOR
        and role.is_active
        and not new_active
        and count_active_super_admins(db) > 0
    ):
        raise AppError(
            "Cannot deactivate the Super Administrator role while any active "
            "user depends on it for admin access",
            status_code=403,
        )

    role.name = name
    role.description = description or None
    role.is_active = new_active

    requested_codes = set(permission_codes or [])
    current_codes = {rp.permission.code for rp in role.role_permissions}
    to_add = requested_codes - current_codes
    to_remove = current_codes - requested_codes

    if to_add:
        code_to_permission = {
            p.code: p for p in db.query(Permission).filter(Permission.code.in_(to_add)).all()
        }
        for code in to_add:
            db.add(RolePermission(role_id=role.id, permission_id=code_to_permission[code].id))
    if to_remove:
        for rp in list(role.role_permissions):
            if rp.permission.code in to_remove:
                db.delete(rp)

    db.commit()
    db.refresh(role)

    after = {
        "name": role.name,
        "description": role.description,
        "is_active": role.is_active,
        "permissions": sorted(rp.permission.code for rp in role.role_permissions),
    }

    record_audit(
        db,
        actor_id=current_user.id,
        action="role_updated",
        entity_type="role",
        entity_id=role.id,
        before=before,
        after=after,
        correlation_id=correlation_id,
        ip_address=ip_address,
    )
    for code in sorted(to_add):
        record_audit(
            db,
            actor_id=current_user.id,
            action="permission_assigned",
            entity_type="role",
            entity_id=role.id,
            after={"permission": code},
            correlation_id=correlation_id,
            ip_address=ip_address,
        )
    for code in sorted(to_remove):
        record_audit(
            db,
            actor_id=current_user.id,
            action="permission_removed",
            entity_type="role",
            entity_id=role.id,
            before={"permission": code},
            correlation_id=correlation_id,
            ip_address=ip_address,
        )

    return RedirectResponse(url=f"/admin/roles/{role.id}", status_code=303)


# --- Audit log ---------------------------------------------------------------------


@router.get("/audit", response_class=HTMLResponse)
def list_audit_log(request: Request, current_user: ManageRoles, db: DbSession):
    entries = db.query(AuditLog).order_by(AuditLog.created_at.desc()).limit(200).all()
    return render(request, "admin/audit.html", {"current_user": current_user, "entries": entries})
