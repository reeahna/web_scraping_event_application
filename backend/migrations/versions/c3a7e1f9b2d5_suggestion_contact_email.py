"""anonymous feedback: contact email and daily counter

Revision ID: c3a7e1f9b2d5
Revises: b8e2d4a6c1f3

Lets visitors who are not signed in send feature ideas and bug reports:

* suggestions.contact_email: the optional address they leave so the team can
  reply. Nullable and empty for every existing row, so nothing stored changes.
* anonymous_feedback_days: one row per UTC day counting their messages, the
  atomic counter behind the daily ceiling on anonymous feedback. New and empty.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3a7e1f9b2d5"
down_revision: str | None = "b8e2d4a6c1f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("suggestions") as batch:
        batch.add_column(sa.Column("contact_email", sa.String(320), nullable=True))
    op.create_table(
        "anonymous_feedback_days",
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("count", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_table("anonymous_feedback_days")
    with op.batch_alter_table("suggestions") as batch:
        batch.drop_column("contact_email")
