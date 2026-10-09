"""requeue events the first geocoding pass could not place

The first pass looked each event up with one query that ran the address,
venue and town together, which rarely matches a campus venue. Lookups now try
several queries, so events it left as needs_review or failed are queued again
and its cached misses are forgotten (cached hits stay).

Revision ID: c4e7a2b9d1f3
Revises: b8d1e4f2a6c9
"""

import sqlalchemy as sa
from alembic import op

revision = "c4e7a2b9d1f3"
down_revision = "b8d1e4f2a6c9"
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
        .where(events.c.geocode_status.in_(["needs_review", "failed"]))
        .values(geocode_status="pending", geocode_attempts=0, geocode_last_error=None)
    )
    cache = sa.table("geocode_cache", sa.column("found", sa.Boolean))
    op.execute(cache.delete().where(cache.c.found.is_(sa.false())))


def downgrade() -> None:
    # Data only: nothing to restore.
    pass
