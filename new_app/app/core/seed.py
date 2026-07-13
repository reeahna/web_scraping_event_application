from sqlalchemy import inspect
from sqlalchemy.orm import Session

from app.core.categories import INITIAL_EVENT_CATEGORIES
from app.core.permissions import DEFAULT_ROLE_PERMISSIONS, PERMISSIONS
from app.models.event_category import EventCategory
from app.models.permission import Permission
from app.models.role import Role
from app.models.role_permission import RolePermission


def seed_permissions(db: Session) -> dict[str, Permission]:
    existing = {p.code: p for p in db.query(Permission).all()}
    for code, description in PERMISSIONS.items():
        if code not in existing:
            perm = Permission(code=code, description=description)
            db.add(perm)
    db.commit()
    return {p.code: p for p in db.query(Permission).all()}


def seed_roles(db: Session) -> dict[str, Role]:
    existing = {r.name: r for r in db.query(Role).all()}
    for name in DEFAULT_ROLE_PERMISSIONS:
        if name not in existing:
            db.add(Role(name=name, description=f"Default '{name}' role"))
    db.commit()
    return {r.name: r for r in db.query(Role).all()}


def seed_role_permissions(
    db: Session, roles: dict[str, Role], permissions: dict[str, Permission]
) -> None:
    existing_pairs = {(rp.role_id, rp.permission_id) for rp in db.query(RolePermission).all()}
    for role_name, codes in DEFAULT_ROLE_PERMISSIONS.items():
        role = roles[role_name]
        for code in codes:
            perm = permissions[code]
            if (role.id, perm.id) not in existing_pairs:
                db.add(RolePermission(role_id=role.id, permission_id=perm.id))
    db.commit()


def seed_defaults(db: Session) -> None:
    """Idempotent: safe to call repeatedly from migrations, the CLI bootstrap
    script, and test fixtures without creating duplicates."""
    permissions = seed_permissions(db)
    roles = seed_roles(db)
    seed_role_permissions(db, roles, permissions)
    seed_event_categories(db)


def seed_event_categories(db: Session) -> dict[str, EventCategory]:
    inspector = inspect(db.get_bind())
    if "event_categories" not in inspector.get_table_names():
        return {}
    columns = {column["name"] for column in inspector.get_columns("event_categories")}
    if not {"description", "display_order"}.issubset(columns):
        # Historical migrations import this live seeder. Phase 5 owns the
        # schema change and its self-contained category seed, so older schema
        # revisions must not query the newer ORM shape.
        return {}

    existing = {category.slug: category for category in db.query(EventCategory).all()}
    for display_order, (name, slug) in enumerate(INITIAL_EVENT_CATEGORIES, start=1):
        if slug not in existing:
            db.add(
                EventCategory(
                    name=name,
                    slug=slug,
                    display_order=display_order,
                    is_active=True,
                )
            )
    db.commit()
    return {category.slug: category for category in db.query(EventCategory).all()}
