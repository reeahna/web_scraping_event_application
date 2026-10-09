"""Make an email address a Super Administrator.

Sign-in is only through Google, Microsoft or Facebook, so this creates no
password. It creates the account (or finds it) and gives it the Super
Administrator role; the person then signs in with a provider that has verified
that address (Google does) and lands in this account.

The SUPERADMIN_EMAIL setting does the same thing automatically on sign-in; this
script is for adding another administrator, or for a site run without that
setting.

Usage (from new_app/, with its venv active, or in Render's Shell):

    python scripts/create_superadmin.py --email you@example.com

Idempotent: running it again for the same address changes nothing.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.email import normalize_email
from app.core.permissions import SUPER_ADMINISTRATOR
from app.core.seed import seed_defaults
from app.database import SessionLocal
from app.models.role import Role
from app.models.user import User
from app.models.user_role import UserRole


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True)
    parser.add_argument("--full-name", default=None)
    args = parser.parse_args()

    db = SessionLocal()
    try:
        seed_defaults(db)
        super_admin_role = db.query(Role).filter(Role.name == SUPER_ADMINISTRATOR).one()

        email = normalize_email(args.email)
        user = db.query(User).filter(User.email == email).first()
        if user is None:
            user = User(email=email, full_name=args.full_name, is_active=True)
            db.add(user)
            db.commit()
            db.refresh(user)
            print(f"Created account {user.email} (id={user.id}).")
        elif not user.is_active:
            user.is_active = True
            db.commit()
            print(f"Re-activated {user.email} (id={user.id}).")

        assigned = (
            db.query(UserRole)
            .filter(UserRole.user_id == user.id, UserRole.role_id == super_admin_role.id)
            .first()
        )
        if assigned is None:
            db.add(UserRole(user_id=user.id, role_id=super_admin_role.id))
            db.commit()
            print(f"{user.email} is now a {SUPER_ADMINISTRATOR}.")
        else:
            print(f"{user.email} is already a {SUPER_ADMINISTRATOR}.")
        print("Sign in with Google (or another provider that verifies the address) to use it.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
