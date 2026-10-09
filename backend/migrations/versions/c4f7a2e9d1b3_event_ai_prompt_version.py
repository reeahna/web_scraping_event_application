"""events.ai_prompt_version: which categorizer prompt labeled the event

Lets a prompt change re-label existing AI-labeled events a batch at a time,
resuming where it stopped, instead of a one-shot --force pass that has to fit
inside one day's free-tier quota.

Revision ID: c4f7a2e9d1b3
Revises: b8d1e4f2a6c9
"""

import sqlalchemy as sa
from alembic import op

revision = "c4f7a2e9d1b3"
down_revision = "b8d1e4f2a6c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("events") as batch_op:
        batch_op.add_column(sa.Column("ai_prompt_version", sa.String(length=16), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("events") as batch_op:
        batch_op.drop_column("ai_prompt_version")
