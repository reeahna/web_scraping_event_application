"""ai label evaluations

Revision ID: e2b6f4a8c3d1
Revises: d7a4c2e9b1f3

Adds ai_label_evaluations: side-by-side tests of AI labeling setups on a fixed
sample of events, run by the scheduler from the AI labeling admin page.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e2b6f4a8c3d1"
down_revision: str | None = "d7a4c2e9b1f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ai_label_evaluations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_by_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("configs", sa.JSON(), nullable=False),
        sa.Column("sample", sa.JSON(), nullable=False),
        sa.Column("progress", sa.JSON(), nullable=False),
        sa.Column("report", sa.JSON()),
        sa.Column("error", sa.String(length=300)),
        sa.Column("requests_made", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_ai_label_evaluations_status", "ai_label_evaluations", ["status"])


def downgrade() -> None:
    op.drop_index("ix_ai_label_evaluations_status", table_name="ai_label_evaluations")
    op.drop_table("ai_label_evaluations")
