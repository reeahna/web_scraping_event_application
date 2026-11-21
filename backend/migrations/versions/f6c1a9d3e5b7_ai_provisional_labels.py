"""ai provisional labels

Revision ID: f6c1a9d3e5b7
Revises: a6d3f1c8e2b4

For the optional backup labeling model (GROQ_FALLBACK_MODEL, off by default):

* events.ai_model: which model produced the event's current AI category.
  NULL for labels made before this.
* events.ai_fallback_attempts: how many times the backup model has failed to
  give the event a usable provisional category. Kept apart from ai_attempts so
  a backup failure never moves an event to needs-review.
* ai_label_batches.provisional: the request went to the backup model, so its
  labels are provisional until the main model verifies them.

Provisional categories themselves are marked by events.category_source =
"ai_provisional" and events.ai_label_status = "provisional"; neither column
changes type.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f6c1a9d3e5b7"
down_revision: str | None = "a6d3f1c8e2b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("events") as batch:
        batch.add_column(sa.Column("ai_model", sa.String(length=100)))
        batch.add_column(
            sa.Column("ai_fallback_attempts", sa.Integer(), nullable=False, server_default="0")
        )
    with op.batch_alter_table("ai_label_batches") as batch:
        batch.add_column(
            sa.Column("provisional", sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade() -> None:
    with op.batch_alter_table("ai_label_batches") as batch:
        batch.drop_column("provisional")
    with op.batch_alter_table("events") as batch:
        batch.drop_column("ai_fallback_attempts")
        batch.drop_column("ai_model")
