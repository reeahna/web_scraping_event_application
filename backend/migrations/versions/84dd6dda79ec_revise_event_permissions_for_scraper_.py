"""revise event permissions for scraper-first model

Revision ID: 84dd6dda79ec
Revises: 9400754b8944
Create Date: 2026-07-13 15:37:40.585721

Drops the general `events.create` / `events.update` permissions — events come
from scrapers, not manual entry, so scraped fields are read-only by default —
and replaces them with narrower, scraper-appropriate actions: reviewing,
archiving, overriding category, correcting location, resolving duplicates,
and viewing provenance. `events.view`, `events.activate`, and `events.delete`
are unaffected.
"""

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.orm import Session

from app.core.seed import seed_defaults

# revision identifiers, used by Alembic.
revision: str = "84dd6dda79ec"
down_revision: str | None = "9400754b8944"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_REMOVED_CODES = ("events.create", "events.update")

# Frozen snapshot of the pre-revision grants, used only by downgrade() so a
# rollback restores exactly what this migration removed — independent of
# whatever app/core/permissions.py looks like by the time someone downgrades.
_OLD_EVENT_GRANTS = {
    "Super Administrator": _REMOVED_CODES,
    "Administrator": _REMOVED_CODES,
    "Editor": _REMOVED_CODES,
}


def upgrade() -> None:
    bind = op.get_bind()
    session = Session(bind=bind)

    session.execute(
        sa.text(
            "DELETE FROM role_permissions WHERE permission_id IN "
            "(SELECT id FROM permissions WHERE code IN :codes)"
        ).bindparams(sa.bindparam("codes", expanding=True)),
        {"codes": list(_REMOVED_CODES)},
    )
    session.execute(
        sa.text("DELETE FROM permissions WHERE code IN :codes").bindparams(
            sa.bindparam("codes", expanding=True)
        ),
        {"codes": list(_REMOVED_CODES)},
    )
    session.commit()

    # Adds the new event permission codes and grants them per the current
    # DEFAULT_ROLE_PERMISSIONS; idempotent, so anything already correct
    # (events.view, events.activate, events.delete, etc.) is left untouched.
    seed_defaults(session)
    session.close()


def downgrade() -> None:
    bind = op.get_bind()
    session = Session(bind=bind)

    new_codes = (
        "events.review",
        "events.archive",
        "events.override_category",
        "events.correct_location",
        "events.resolve_duplicates",
        "events.view_provenance",
    )
    session.execute(
        sa.text(
            "DELETE FROM role_permissions WHERE permission_id IN "
            "(SELECT id FROM permissions WHERE code IN :codes)"
        ).bindparams(sa.bindparam("codes", expanding=True)),
        {"codes": list(new_codes)},
    )
    session.execute(
        sa.text("DELETE FROM permissions WHERE code IN :codes").bindparams(
            sa.bindparam("codes", expanding=True)
        ),
        {"codes": list(new_codes)},
    )

    now = datetime.now(UTC)
    for code in _REMOVED_CODES:
        session.execute(
            sa.text(
                "INSERT INTO permissions (code, description, created_at, updated_at) "
                "VALUES (:code, :description, :now, :now)"
            ),
            {
                "code": code,
                "description": code.split(".")[-1].capitalize() + " events",
                "now": now,
            },
        )
    session.commit()

    permission_ids = dict(
        session.execute(
            sa.text("SELECT code, id FROM permissions WHERE code IN :codes").bindparams(
                sa.bindparam("codes", expanding=True)
            ),
            {"codes": list(_REMOVED_CODES)},
        ).all()
    )
    role_ids = dict(
        session.execute(
            sa.text("SELECT name, id FROM roles WHERE name IN :names").bindparams(
                sa.bindparam("names", expanding=True)
            ),
            {"names": list(_OLD_EVENT_GRANTS.keys())},
        ).all()
    )
    for role_name, codes in _OLD_EVENT_GRANTS.items():
        role_id = role_ids.get(role_name)
        if role_id is None:
            continue
        for code in codes:
            session.execute(
                sa.text(
                    "INSERT INTO role_permissions "
                    "(role_id, permission_id, created_at, updated_at) "
                    "VALUES (:role_id, :permission_id, :now, :now)"
                ),
                {"role_id": role_id, "permission_id": permission_ids[code], "now": now},
            )
    session.commit()
    session.close()
