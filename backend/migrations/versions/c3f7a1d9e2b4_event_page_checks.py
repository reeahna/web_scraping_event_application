"""event page checks

Revision ID: c3f7a1d9e2b4
Revises: b8e2d4a6c1f3

Adds event_page_checks: event pages the venue lookup fetched successfully
without finding a venue, so later runs skip them for a few days and the
lookup's page budget reaches other events. Purely additive; the table starts
empty and nothing a visitor sees changes.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3f7a1d9e2b4"
down_revision: str | None = "b8e2d4a6c1f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "event_page_checks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("website_id", sa.Integer(), nullable=False),
        sa.Column("url_hash", sa.String(64), nullable=False),
        sa.Column("url", sa.String(2000), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["website_id"], ["websites.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("website_id", "url_hash", name="uq_event_page_checks_website_url"),
    )
    op.create_index("ix_event_page_checks_website_id", "event_page_checks", ["website_id"])


def downgrade() -> None:
    op.drop_index("ix_event_page_checks_website_id", table_name="event_page_checks")
    op.drop_table("event_page_checks")
