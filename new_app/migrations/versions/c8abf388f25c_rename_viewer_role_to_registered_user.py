"""rename viewer role to registered user

Revision ID: c8abf388f25c
Revises: ac034c0f9ec1
Create Date: 2026-07-13 16:57:50.736953

Fully self-contained: does not import app.core.permissions or app.core.seed,
so this migration's behavior (especially downgrade) never depends on how the
live application code defines roles/permissions in the future.

Deterministic conflict handling:
- Viewer exists, Registered User doesn't -> rename in place (same row id, so
  every UserRole/RolePermission referencing it stays valid with zero data
  movement).
- Both exist (unexpected) -> merge Viewer's user assignments into Registered
  User (skipping any pair that would violate the unique constraint), strip
  Viewer's permission grants, delete the Viewer row.
- Neither exists -> create Registered User fresh.
- Only Registered User exists -> already in the desired state.

Whatever path is taken, the migration then guarantees Registered User ends
up with zero permission grants (the public self-registration role must not
receive any elevated permission by default).
"""

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.orm import Session

# revision identifiers, used by Alembic.
revision: str = "c8abf388f25c"
down_revision: str | None = "ac034c0f9ec1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_NAME = "Viewer"
_NEW_NAME = "Registered User"
_NEW_DESCRIPTION = (
    "Default role for public self-registration. Grants no elevated permissions by default."
)
_OLD_DESCRIPTION = "Default 'Viewer' role"

# Frozen snapshot of Viewer's original permission grants, used only by
# downgrade() — deliberately not read from app.core.permissions.
_OLD_VIEWER_PERMISSION_CODES = (
    "cities.view",
    "sites.view",
    "events.view",
    "users.view",
    "reports.view",
)


def _now() -> datetime:
    return datetime.now(UTC)


def upgrade() -> None:
    bind = op.get_bind()
    session = Session(bind=bind)

    viewer_id = session.execute(
        sa.text("SELECT id FROM roles WHERE name = :name"), {"name": _OLD_NAME}
    ).scalar()
    registered_user_id = session.execute(
        sa.text("SELECT id FROM roles WHERE name = :name"), {"name": _NEW_NAME}
    ).scalar()

    if viewer_id is not None and registered_user_id is None:
        # Common case: rename in place, preserving the row's id.
        session.execute(
            sa.text(
                "UPDATE roles SET name = :new_name, description = :description, "
                "updated_at = :now WHERE id = :role_id"
            ),
            {
                "new_name": _NEW_NAME,
                "description": _NEW_DESCRIPTION,
                "now": _now(),
                "role_id": viewer_id,
            },
        )
        registered_user_id = viewer_id

    elif viewer_id is not None and registered_user_id is not None:
        # Unexpected: both rows exist. Merge user assignments, then remove Viewer.
        existing_user_ids = {
            row[0]
            for row in session.execute(
                sa.text("SELECT user_id FROM user_roles WHERE role_id = :rid"),
                {"rid": registered_user_id},
            ).all()
        }
        viewer_user_rows = session.execute(
            sa.text("SELECT id, user_id FROM user_roles WHERE role_id = :rid"),
            {"rid": viewer_id},
        ).all()
        for ur_id, user_id in viewer_user_rows:
            if user_id in existing_user_ids:
                # User already holds Registered User too — drop the duplicate
                # Viewer assignment rather than violate the unique constraint.
                session.execute(sa.text("DELETE FROM user_roles WHERE id = :id"), {"id": ur_id})
            else:
                session.execute(
                    sa.text("UPDATE user_roles SET role_id = :new_rid WHERE id = :id"),
                    {"new_rid": registered_user_id, "id": ur_id},
                )
        session.execute(
            sa.text("DELETE FROM role_permissions WHERE role_id = :rid"), {"rid": viewer_id}
        )
        session.execute(sa.text("DELETE FROM roles WHERE id = :rid"), {"rid": viewer_id})

    elif viewer_id is None and registered_user_id is None:
        # Neither exists yet — create Registered User fresh, no grants.
        session.execute(
            sa.text(
                "INSERT INTO roles (name, description, is_active, created_at, updated_at) "
                "VALUES (:name, :description, 1, :now, :now)"
            ),
            {"name": _NEW_NAME, "description": _NEW_DESCRIPTION, "now": _now()},
        )
        registered_user_id = session.execute(
            sa.text("SELECT id FROM roles WHERE name = :name"), {"name": _NEW_NAME}
        ).scalar()

    # else: Registered User already exists and Viewer doesn't — already done.

    # Guarantee the invariant regardless of which path was taken above: the
    # public self-registration role has zero permission grants by default.
    if registered_user_id is not None:
        session.execute(
            sa.text("DELETE FROM role_permissions WHERE role_id = :rid"),
            {"rid": registered_user_id},
        )

    session.commit()
    session.close()


def downgrade() -> None:
    bind = op.get_bind()
    session = Session(bind=bind)

    registered_user_id = session.execute(
        sa.text("SELECT id FROM roles WHERE name = :name"), {"name": _NEW_NAME}
    ).scalar()
    if registered_user_id is None:
        session.close()
        return

    now = _now()
    session.execute(
        sa.text(
            "UPDATE roles SET name = :old_name, description = :description, "
            "updated_at = :now WHERE id = :role_id"
        ),
        {
            "old_name": _OLD_NAME,
            "description": _OLD_DESCRIPTION,
            "now": now,
            "role_id": registered_user_id,
        },
    )

    permission_ids = dict(
        session.execute(
            sa.text("SELECT code, id FROM permissions WHERE code IN :codes").bindparams(
                sa.bindparam("codes", expanding=True)
            ),
            {"codes": list(_OLD_VIEWER_PERMISSION_CODES)},
        ).all()
    )
    existing_codes = {
        code
        for (code,) in session.execute(
            sa.text(
                "SELECT p.code FROM role_permissions rp "
                "JOIN permissions p ON p.id = rp.permission_id "
                "WHERE rp.role_id = :rid"
            ),
            {"rid": registered_user_id},
        ).all()
    }
    for code in _OLD_VIEWER_PERMISSION_CODES:
        if code in existing_codes or code not in permission_ids:
            continue
        session.execute(
            sa.text(
                "INSERT INTO role_permissions (role_id, permission_id, created_at, updated_at) "
                "VALUES (:rid, :pid, :now, :now)"
            ),
            {"rid": registered_user_id, "pid": permission_ids[code], "now": now},
        )

    session.commit()
    session.close()
