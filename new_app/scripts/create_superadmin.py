"""Development command to create (or promote) the first Super Administrator.

Usage (from new_app/, with its venv active):

    python scripts/create_superadmin.py --email reeahna9@gmail.com --password "12345678"

Idempotent: if the user already exists, it updates their password and ensures
they hold the Super Administrator role rather than creating a duplicate.
"""

import argparse
import sys
from pathlib import Path 

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.email import normalize_email
from app.core.permissions import SUPER_ADMINISTRATOR
from app.core.security import hash_password
from app.core.seed import seed_defaults
from app.database import SessionLocal
from app.models.role import Role
from app.models.user import User
from app.models.user_role import UserRole


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--full-name", default=None)
    args = parser.parse_args()

    if len(args.password) < 8:
        print("Password must be at least 8 characters.", file=sys.stderr)
        raise SystemExit(1)

    db = SessionLocal()
    try:
        seed_defaults(db)

        super_admin_role = db.query(Role).filter(Role.name == SUPER_ADMINISTRATOR).one()

        email = normalize_email(args.email)
        user = db.query(User).filter(User.email == email).first()
        if user is None:
            user = User(
                email=email,
                full_name=args.full_name,
                hashed_password=hash_password(args.password),
                is_active=True,
            )
            db.add(user)
            db.commit()
            db.refresh(user)
            print(f"Created user {user.email} (id={user.id}).")
        else:
            user.hashed_password = hash_password(args.password)
            user.is_active = True
            db.commit()
            print(f"Updated existing user {user.email} (id={user.id}).")

        already_assigned = (
            db.query(UserRole)
            .filter(UserRole.user_id == user.id, UserRole.role_id == super_admin_role.id)
            .first()
        )
        if already_assigned is None:
            db.add(UserRole(user_id=user.id, role_id=super_admin_role.id))
            db.commit()
            print(f"Assigned '{SUPER_ADMINISTRATOR}' role to {user.email}.")
        else:
            print(f"{user.email} already holds '{SUPER_ADMINISTRATOR}'.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
