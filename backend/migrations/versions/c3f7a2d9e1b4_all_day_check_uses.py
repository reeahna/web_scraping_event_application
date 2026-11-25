"""all day check uses

Revision ID: c3f7a2d9e1b4
Revises: c3f7a1d9e2b4

Adds all_day_check_uses: one row per all-day backfill Check that has been
applied, keyed by the Check's audit entry id. The primary key makes a Check
usable once, enforced by the database on SQLite and PostgreSQL. A new, empty
table: no existing row changes.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3f7a2d9e1b4"
down_revision: str | None = "c3f7a1d9e2b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "all_day_check_uses",
        sa.Column("check_id", sa.Integer(), sa.ForeignKey("audit_logs.id"), primary_key=True),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("all_day_check_uses")
