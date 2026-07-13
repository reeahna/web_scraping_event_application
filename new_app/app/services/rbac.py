from collections.abc import Callable

from sqlalchemy.orm import Session

from app.core.exceptions import AppError
from app.core.permissions import SUPER_ADMINISTRATOR
from app.dependencies import CurrentUser, DbSession
from app.models.permission import Permission
from app.models.role import Role
from app.models.role_permission import RolePermission
from app.models.user import User
from app.models.user_role import UserRole


def get_effective_permissions(db: Session, user: User) -> set[str]:
    rows = (
        db.query(Permission.code)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .join(Role, Role.id == RolePermission.role_id)
        .join(UserRole, UserRole.role_id == Role.id)
        .filter(UserRole.user_id == user.id, Role.is_active.is_(True))
        .distinct()
        .all()
    )
    return {code for (code,) in rows}


def user_has_permission(db: Session, user: User, code: str) -> bool:
    return code in get_effective_permissions(db, user)


def require_permission(code: str) -> Callable[..., User]:
    def dependency(current_user: CurrentUser, db: DbSession) -> User:
        if not user_has_permission(db, current_user, code):
            raise AppError("Forbidden: missing permission", status_code=403)
        return current_user

    return dependency


def count_active_super_admins(db: Session, *, exclude_user_id: int | None = None) -> int:
    query = (
        db.query(User.id)
        .join(UserRole, UserRole.user_id == User.id)
        .join(Role, Role.id == UserRole.role_id)
        .filter(
            Role.name == SUPER_ADMINISTRATOR,
            Role.is_active.is_(True),
            User.is_active.is_(True),
        )
        .distinct()
    )
    if exclude_user_id is not None:
        query = query.filter(User.id != exclude_user_id)
    return query.count()


def assert_not_last_super_admin(db: Session, user: User) -> None:
    """Raise 403 if removing admin access from `user` would leave zero active
    Super Administrators."""
    is_super_admin = (
        db.query(UserRole)
        .join(Role, Role.id == UserRole.role_id)
        .filter(
            UserRole.user_id == user.id,
            Role.name == SUPER_ADMINISTRATOR,
            Role.is_active.is_(True),
        )
        .first()
        is not None
    )
    if not is_super_admin:
        return
    if count_active_super_admins(db, exclude_user_id=user.id) == 0:
        raise AppError(
            "Cannot remove the last active Super Administrator's admin access",
            status_code=403,
        )
