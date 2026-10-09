"""phase 16 legacy migration status

Revision ID: d5f1a3c8b940
Revises: c9d4a1b6e072

Adds three columns to `websites` recording the legacy-migration review outcome:
`legacy_migration_status` (pending | migrated | unavailable), the mapped legacy
source name, and the migrated timestamp. Additive and server-defaulted; never
affects live extraction, and touches nothing in the legacy database.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d5f1a3c8b940"
down_revision: str | None = "c9d4a1b6e072"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "websites",
        sa.Column(
            "legacy_migration_status", sa.String(length=16), nullable=False,
            server_default=sa.text("'pending'"),
        ),
    )
    op.add_column("websites", sa.Column("legacy_source_name", sa.String(length=255), nullable=True))
    op.add_column(
        "websites", sa.Column("legacy_migrated_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("websites", "legacy_migrated_at")
    op.drop_column("websites", "legacy_source_name")
    op.drop_column("websites", "legacy_migration_status")
