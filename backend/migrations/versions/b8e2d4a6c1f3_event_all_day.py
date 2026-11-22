"""event all day

Revision ID: b8e2d4a6c1f3
Revises: f6c1a9d3e5b7

Adds events.all_day: the source says the event lasts the whole day. Every
existing row starts False, so nothing a visitor sees changes until a later
import (or the separate, reviewed backfill in scripts/backfill_all_day.py)
sets it from the source's own all-day signal. Date-only rows are not assumed
to be all-day: a missing time can also mean "time not known".
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b8e2d4a6c1f3"
down_revision: str | None = "f6c1a9d3e5b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("events") as batch:
        batch.add_column(
            sa.Column("all_day", sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade() -> None:
    with op.batch_alter_table("events") as batch:
        batch.drop_column("all_day")
