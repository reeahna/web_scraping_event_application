"""phase 11 asynchronous geocoding

Revision ID: a1c8e6f4920d
Revises: f4b2d90c1a57

Adds derived-coordinate + queue columns to ``events`` and a ``geocode_cache``
table keyed by normalized-address hash.

Self-contained (imports nothing from the application) and additive: the new
event columns are server-defaulted (coordinates null, status 'pending',
attempts 0), and the cache table is new. Installing this revision changes no
existing coordinate and — because geocoding is disabled by default and only a
dedicated process ever drains the queue — makes no third-party request on its
own.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1c8e6f4920d"
down_revision: str | None = "f4b2d90c1a57"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("events", sa.Column("geocoded_latitude", sa.Float(), nullable=True))
    op.add_column("events", sa.Column("geocoded_longitude", sa.Float(), nullable=True))
    op.add_column(
        "events",
        sa.Column(
            "geocode_status", sa.String(length=32), nullable=False,
            server_default=sa.text("'pending'"),
        ),
    )
    op.add_column(
        "events",
        sa.Column("geocode_attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column("events", sa.Column("geocode_last_error", sa.String(length=500), nullable=True))
    op.add_column("events", sa.Column("geocoded_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_events_geocode_status", "events", ["geocode_status"], unique=False)

    op.create_table(
        "geocode_cache",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("address_hash", sa.String(length=64), nullable=False),
        sa.Column("normalized_address", sa.String(length=1000), nullable=False),
        sa.Column("found", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=1000), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("address_hash", name="uq_geocode_cache_address_hash"),
    )
    op.create_index(
        "ix_geocode_cache_address_hash", "geocode_cache", ["address_hash"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_geocode_cache_address_hash", table_name="geocode_cache")
    op.drop_table("geocode_cache")
    op.drop_index("ix_events_geocode_status", table_name="events")
    op.drop_column("events", "geocoded_at")
    op.drop_column("events", "geocode_last_error")
    op.drop_column("events", "geocode_attempts")
    op.drop_column("events", "geocode_status")
    op.drop_column("events", "geocoded_longitude")
    op.drop_column("events", "geocoded_latitude")
