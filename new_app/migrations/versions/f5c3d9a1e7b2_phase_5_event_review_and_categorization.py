"""phase 5 event review and deterministic categorization

Revision ID: f5c3d9a1e7b2
Revises: c8abf388f25c

This migration is deliberately self-contained. Its category seed and defaults
are frozen here rather than imported from live application code.
"""

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "f5c3d9a1e7b2"
down_revision: str | None = "c8abf388f25c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CATEGORIES = (
    ("Arts and Culture", "arts-and-culture"),
    ("Music", "music"),
    ("Sports", "sports"),
    ("Family", "family"),
    ("Education", "education"),
    ("Community", "community"),
    ("Food and Drink", "food-and-drink"),
    ("Business", "business"),
    ("Nightlife", "nightlife"),
    ("Outdoors", "outdoors"),
    ("Government", "government"),
    ("Religious", "religious"),
    ("Health and Wellness", "health-and-wellness"),
    ("Other", "other"),
)


def upgrade() -> None:
    op.add_column("event_categories", sa.Column("description", sa.String(500), nullable=True))
    op.add_column(
        "event_categories",
        sa.Column("display_order", sa.Integer(), nullable=False, server_default="0"),
    )

    op.create_table(
        "categorization_rules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False, unique=True),
        sa.Column("rule_type", sa.String(32), nullable=False),
        sa.Column("category_id", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("website_id", sa.Integer(), nullable=True),
        sa.Column("source_category_value", sa.String(255), nullable=True),
        sa.Column("pattern", sa.String(500), nullable=True),
        sa.Column("is_regex", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("case_sensitive", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["category_id"], ["event_categories.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["website_id"], ["websites.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_categorization_rules_rule_type", "categorization_rules", ["rule_type"])
    op.create_index("ix_categorization_rules_category_id", "categorization_rules", ["category_id"])
    op.create_index("ix_categorization_rules_is_active", "categorization_rules", ["is_active"])
    op.create_index("ix_categorization_rules_website_id", "categorization_rules", ["website_id"])

    with op.batch_alter_table("events") as batch_op:
        batch_op.add_column(sa.Column("normalized_title", sa.String(500), nullable=True))
        batch_op.add_column(sa.Column("source_category", sa.String(255), nullable=True))
        batch_op.add_column(sa.Column("timezone", sa.String(64), nullable=True))
        batch_op.add_column(
            sa.Column("origin", sa.String(32), nullable=False, server_default="extracted")
        )
        batch_op.add_column(
            sa.Column("review_status", sa.String(32), nullable=False, server_default="needs_review")
        )
        batch_op.add_column(
            sa.Column(
                "duplicate_status", sa.String(32), nullable=False, server_default="not_reviewed"
            )
        )
        batch_op.add_column(
            sa.Column(
                "category_source", sa.String(32), nullable=False, server_default="uncategorized"
            )
        )
        batch_op.add_column(sa.Column("categorization_rule_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("category_override_id", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column("category_overridden_by_user_id", sa.Integer(), nullable=True)
        )
        batch_op.add_column(sa.Column("category_overridden_at", sa.DateTime(timezone=True)))
        batch_op.add_column(sa.Column("category_override_reason", sa.String(500)))
        batch_op.add_column(sa.Column("corrected_venue", sa.String(500)))
        batch_op.add_column(sa.Column("corrected_address", sa.String(1000)))
        batch_op.add_column(sa.Column("corrected_latitude", sa.Float()))
        batch_op.add_column(sa.Column("corrected_longitude", sa.Float()))
        batch_op.add_column(sa.Column("location_corrected_by_user_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("location_corrected_at", sa.DateTime(timezone=True)))
        batch_op.add_column(sa.Column("duplicate_preferred_event_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_events_categorization_rule",
            "categorization_rules",
            ["categorization_rule_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_foreign_key(
            "fk_events_category_override",
            "event_categories",
            ["category_override_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_foreign_key(
            "fk_events_category_override_user",
            "users",
            ["category_overridden_by_user_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_foreign_key(
            "fk_events_location_correction_user",
            "users",
            ["location_corrected_by_user_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_foreign_key(
            "fk_events_duplicate_preferred",
            "events",
            ["duplicate_preferred_event_id"],
            ["id"],
            ondelete="SET NULL",
        )

    op.create_index("ix_events_normalized_title", "events", ["normalized_title"])
    op.create_index("ix_events_review_status", "events", ["review_status"])
    op.create_index("ix_events_duplicate_status", "events", ["duplicate_status"])

    bind = op.get_bind()
    now = datetime.now(UTC)
    for display_order, (name, slug) in enumerate(_CATEGORIES, start=1):
        exists = bind.execute(
            sa.text("SELECT id FROM event_categories WHERE name = :name OR slug = :slug LIMIT 1"),
            {"name": name, "slug": slug},
        ).scalar()
        if exists is None:
            bind.execute(
                sa.text(
                    "INSERT INTO event_categories "
                    "(name, slug, description, display_order, is_active, created_at, updated_at) "
                    "VALUES (:name, :slug, NULL, :display_order, 1, :now, :now)"
                ),
                {
                    "name": name,
                    "slug": slug,
                    "display_order": display_order,
                    "now": now,
                },
            )

    bind.execute(
        sa.text(
            "UPDATE events SET normalized_title = lower(trim(title)) WHERE normalized_title IS NULL"
        )
    )


def downgrade() -> None:
    op.drop_index("ix_events_duplicate_status", table_name="events")
    op.drop_index("ix_events_review_status", table_name="events")
    op.drop_index("ix_events_normalized_title", table_name="events")

    with op.batch_alter_table("events") as batch_op:
        batch_op.drop_constraint("fk_events_duplicate_preferred", type_="foreignkey")
        batch_op.drop_constraint("fk_events_location_correction_user", type_="foreignkey")
        batch_op.drop_constraint("fk_events_category_override_user", type_="foreignkey")
        batch_op.drop_constraint("fk_events_category_override", type_="foreignkey")
        batch_op.drop_constraint("fk_events_categorization_rule", type_="foreignkey")
        for column in (
            "duplicate_preferred_event_id",
            "location_corrected_at",
            "location_corrected_by_user_id",
            "corrected_longitude",
            "corrected_latitude",
            "corrected_address",
            "corrected_venue",
            "category_override_reason",
            "category_overridden_at",
            "category_overridden_by_user_id",
            "category_override_id",
            "categorization_rule_id",
            "category_source",
            "duplicate_status",
            "review_status",
            "origin",
            "timezone",
            "source_category",
            "normalized_title",
        ):
            batch_op.drop_column(column)

    op.drop_index("ix_categorization_rules_website_id", table_name="categorization_rules")
    op.drop_index("ix_categorization_rules_is_active", table_name="categorization_rules")
    op.drop_index("ix_categorization_rules_category_id", table_name="categorization_rules")
    op.drop_index("ix_categorization_rules_rule_type", table_name="categorization_rules")
    op.drop_table("categorization_rules")
    op.drop_column("event_categories", "display_order")
    op.drop_column("event_categories", "description")
