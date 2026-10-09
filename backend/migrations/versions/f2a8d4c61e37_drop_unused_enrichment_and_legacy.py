"""drop the unused AI-enrichment table and legacy-migration columns

The AI enrichment feature (Phase 17) was never called by the app, and the
legacy comparison page (Phase 16) compared against a legacy database that has
been removed; both were deleted from the code. This removes what they left in
the schema: the event_enrichments table and the three websites.legacy_*
columns. Downgrade recreates both, empty.

Revision ID: f2a8d4c61e37
Revises: e1f3c7a92b84
"""

import sqlalchemy as sa
from alembic import op

revision = "f2a8d4c61e37"
down_revision = "e1f3c7a92b84"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_event_enrichments_input_hash", table_name="event_enrichments")
    op.drop_index("ix_event_enrichments_event_id", table_name="event_enrichments")
    op.drop_table("event_enrichments")

    # batch mode: SQLite cannot drop a column in place.
    with op.batch_alter_table("websites") as batch:
        batch.drop_column("legacy_migrated_at")
        batch.drop_column("legacy_source_name")
        batch.drop_column("legacy_migration_status")


def downgrade() -> None:
    with op.batch_alter_table("websites") as batch:
        batch.add_column(
            sa.Column(
                "legacy_migration_status", sa.String(length=16), nullable=False,
                server_default=sa.text("'pending'"),
            )
        )
        batch.add_column(sa.Column("legacy_source_name", sa.String(length=255), nullable=True))
        batch.add_column(
            sa.Column("legacy_migrated_at", sa.DateTime(timezone=True), nullable=True)
        )

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
