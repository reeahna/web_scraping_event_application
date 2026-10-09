"""requeue events whose street address found no match

Addresses that name their own town are now searched as written, without the
event's town appended, and zip codes and floor numbers are dropped. Events
left as needs_review are queued again and cached misses are forgotten
(cached hits stay).

Revision ID: d2a8f5c3e9b1
Revises: c4e7a2b9d1f3
"""

import sqlalchemy as sa
from alembic import op

revision = "d2a8f5c3e9b1"
down_revision = "c4e7a2b9d1f3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    events = sa.table(
        "events",
        sa.column("geocode_status", sa.String),
        sa.column("geocode_attempts", sa.Integer),
        sa.column("geocode_last_error", sa.String),
    )
    op.execute(
        events.update()
        .where(events.c.geocode_status == "needs_review")
        .values(geocode_status="pending", geocode_attempts=0, geocode_last_error=None)
    )
    cache = sa.table("geocode_cache", sa.column("found", sa.Boolean))
    op.execute(cache.delete().where(cache.c.found.is_(sa.false())))


def downgrade() -> None:
    # Data only: nothing to restore.
    pass
