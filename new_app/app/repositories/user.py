from sqlalchemy.orm import Session

from app.core.email import normalize_email
from app.models.user import User


def get_user(db: Session, user_id: int) -> User | None:
    return db.get(User, user_id)


def get_user_by_email(db: Session, email: str) -> User | None:
    """`email` should already be normalized by the caller — normalizing again
    here too so this is safe to call with a raw, un-normalized address."""
    return db.query(User).filter(User.email == normalize_email(email)).first()


def create_user(
    db: Session, *, email: str, hashed_password: str, full_name: str | None = None
) -> User:
    """Always creates the user active — callers decide role assignment
    separately (see app.services.registration)."""
    user = User(
        email=normalize_email(email),
        full_name=full_name,
        hashed_password=hashed_password,
        is_active=True,
    )
    db.add(user)
    # The caller owns the transaction so account creation and its initial role
    # assignment can succeed or fail as one unit.
    db.flush()
    return user
