"""The test user: an ordinary Registered User account for trying the site
while signed in, in development or staging, without a Google, Microsoft or
Facebook account.

Only reachable when TEST_LOGIN_ENABLED is on, which the settings refuse in
production. The account is created on first use, so deleting it from /account
simply starts the next test sign-in with a fresh one.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.permissions import REGISTERED_USER
from app.models.role import Role
from app.models.user import User
from app.models.user_role import UserRole
from app.repositories.user import create_user, get_user_by_email

TEST_USER_NAME = "Test User"


def ensure_test_user(db: Session, email: str) -> User:
    """The test account for `email`, created (with the Registered User role)
    if it does not exist yet, and re-enabled if an admin disabled it."""
    user = get_user_by_email(db, email)
    if user is None:
        user = create_user(db, email=email, full_name=TEST_USER_NAME)
        role = db.scalar(
            select(Role).where(Role.name == REGISTERED_USER, Role.is_active.is_(True))
        )
        if role is not None:
            db.add(UserRole(user_id=user.id, role_id=role.id))
    elif not user.is_active:
        user.is_active = True
    db.commit()
    db.refresh(user)
    return user
