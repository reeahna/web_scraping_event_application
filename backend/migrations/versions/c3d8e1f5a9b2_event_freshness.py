"""event freshness

Revision ID: c3d8e1f5a9b2
Revises: b5e8d2c4f1a7

Adds events.content_updated_at (when visitor-facing details last changed) and
per-run outcome counts on extraction_runs (changed, cancelled, missing).

Existing events keep content_updated_at NULL: events.updated_at moves on every
scrape, so it cannot say when details last changed, and guessing would put a
wrong date on the public page. The value fills in as sources change, new events
arrive and administrators correct them.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3d8e1f5a9b2"
down_revision: str | None = "b5e8d2c4f1a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RUN_COUNTS = ("events_changed", "events_cancelled", "events_missing")


def upgrade() -> None:
    with op.batch_alter_table("events") as batch:
        batch.add_column(sa.Column("content_updated_at", sa.DateTime(timezone=True)))
    with op.batch_alter_table("extraction_runs") as batch:
        for name in _RUN_COUNTS:
            batch.add_column(
                sa.Column(name, sa.Integer(), nullable=False, server_default="0")
            )


def downgrade() -> None:
    with op.batch_alter_table("extraction_runs") as batch:
        for name in reversed(_RUN_COUNTS):
            batch.drop_column(name)
    with op.batch_alter_table("events") as batch:
        batch.drop_column("content_updated_at")
