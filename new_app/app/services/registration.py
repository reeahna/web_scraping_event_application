from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import AppError
from app.core.permissions import REGISTERED_USER
from app.core.security import hash_password
from app.models.role import Role
from app.models.user import User
from app.models.user_role import UserRole
from app.repositories.user import create_user, get_user_by_email
from app.schemas.registration import RegistrationCreate
from app.services.audit import record_audit


class EmailAlreadyRegisteredError(AppError):
    def __init__(self) -> None:
        super().__init__("An account with this email address already exists.", status_code=409)


def register_user(
    db: Session,
    data: RegistrationCreate,
    *,
    correlation_id: str | None = None,
    ip_address: str | None = None,
) -> User:
    """Create one active account with the zero-permission registration role."""
    if get_user_by_email(db, data.email) is not None:
        raise EmailAlreadyRegisteredError

    role = db.query(Role).filter(Role.name == REGISTERED_USER, Role.is_active.is_(True)).first()
    if role is None:
        # Fail before inserting a user rather than leave an account without its
        # required default role.
        raise AppError("Registration is temporarily unavailable.", status_code=500)

    hashed_password = hash_password(data.password)

    try:
        user = create_user(
            db,
            email=data.email,
            hashed_password=hashed_password,
            full_name=data.display_name,
        )
        db.add(UserRole(user_id=user.id, role_id=role.id))
        db.commit()
        db.refresh(user)
    except IntegrityError as exc:
        # Covers the database uniqueness constraint if two requests race after
        # the service-level check above.
        db.rollback()
        raise EmailAlreadyRegisteredError from exc

    record_audit(
        db,
        actor_id=user.id,
        action="user_registered",
        entity_type="user",
        entity_id=user.id,
        after={"email": user.email},
        correlation_id=correlation_id,
        ip_address=ip_address,
    )
    record_audit(
        db,
        actor_id=user.id,
        action="default_registered_user_role_assigned",
        entity_type="user",
        entity_id=user.id,
        after={"role": REGISTERED_USER},
        correlation_id=correlation_id,
        ip_address=ip_address,
    )
    return user
