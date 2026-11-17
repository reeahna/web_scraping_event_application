"""visitor log

Revision ID: b5e8d2c4f1a7
Revises: a7c4e2f9b318

Adds the visitor log tables: site_visitor_days (one row per hashed visitor per
day), site_page_days (views per page per day) and visit_salts (the current
day's random salt). No IP address or cookie is stored.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b5e8d2c4f1a7"
down_revision: str | None = "a7c4e2f9b318"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "site_visitor_days",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("visitor_hash", sa.String(32), nullable=False),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("page_views", sa.Integer(), nullable=False),
        sa.Column("device", sa.String(16), nullable=False),
        sa.Column("referrer_host", sa.String(255), nullable=True),
        sa.Column("towns", sa.String(500), nullable=True),
        sa.Column("last_path", sa.String(255), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("day", "visitor_hash", name="uq_site_visitor_day"),
    )
    op.create_index("ix_site_visitor_days_day", "site_visitor_days", ["day"])
    op.create_index("ix_site_visitor_days_user_id", "site_visitor_days", ["user_id"])

    op.create_table(
        "site_page_days",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("path", sa.String(255), nullable=False),
        sa.Column("town_slug", sa.String(120), nullable=True),
        sa.Column("views", sa.Integer(), nullable=False),
        sa.UniqueConstraint("day", "path", name="uq_site_page_day"),
    )
    op.create_index("ix_site_page_days_day", "site_page_days", ["day"])
    op.create_index("ix_site_page_days_town_slug", "site_page_days", ["town_slug"])

    op.create_table(
        "visit_salts",
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("salt", sa.String(64), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("visit_salts")
    op.drop_index("ix_site_page_days_town_slug", table_name="site_page_days")
    op.drop_index("ix_site_page_days_day", table_name="site_page_days")
    op.drop_table("site_page_days")
    op.drop_index("ix_site_visitor_days_user_id", table_name="site_visitor_days")
    op.drop_index("ix_site_visitor_days_day", table_name="site_visitor_days")
    op.drop_table("site_visitor_days")
