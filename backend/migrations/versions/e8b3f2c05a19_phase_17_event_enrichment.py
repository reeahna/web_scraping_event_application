"""phase 17 ai event enrichment

Revision ID: e8b3f2c05a19
Revises: d5f1a3c8b940

Adds event_enrichments: advisory AI suggestions for an event (category, tags,
summary, audience, family-friendly, duplicate candidates), cached by
(event_id, prompt_version, input_hash). Additive; never affects an event's
authoritative fields, and creates no enrichment on its own (AI is disabled by
default).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e8b3f2c05a19"
down_revision: str | None = "d5f1a3c8b940"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "event_enrichments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("prompt_version", sa.String(length=16), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column(
            "status", sa.String(length=16), nullable=False,
            server_default=sa.text("'suggested'"),
        ),
        sa.Column("category_suggestion", sa.String(length=255), nullable=True),
        sa.Column("tags", sa.JSON(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("audience", sa.String(length=255), nullable=True),
        sa.Column("family_friendly", sa.Boolean(), nullable=True),
        sa.Column("extra", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "event_id", "prompt_version", "input_hash", name="uq_event_enrichment_cache"
        ),
    )
    op.create_index(
        "ix_event_enrichments_event_id", "event_enrichments", ["event_id"], unique=False
    )
    op.create_index(
        "ix_event_enrichments_input_hash", "event_enrichments", ["input_hash"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_event_enrichments_input_hash", table_name="event_enrichments")
    op.drop_index("ix_event_enrichments_event_id", table_name="event_enrichments")
    op.drop_table("event_enrichments")
