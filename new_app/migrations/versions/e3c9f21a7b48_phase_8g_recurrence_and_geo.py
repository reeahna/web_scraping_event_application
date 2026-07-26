"""phase 8g recurrence identity and shared quality policy

Revision ID: e3c9f21a7b48
Revises: d7a1c4e83b60

Adds the columns behind Phase 8G's shared extraction capabilities:

* On ``events``: recurrence/occurrence identity (``occurrence_id``,
  ``recurrence_parent_id``, ``is_recurrence_parent``) and a cancellation flag
  (``is_cancelled``). A cancelled occurrence is hidden by deactivation, never
  deleted, so these are additive and never remove history.
* On ``auto_onboarding_policies``: two date-range gates and two
  geographic-filter gates, all defaulted off/neutral so an existing policy's
  behaviour is unchanged.

Self-contained by design: it imports nothing from the application (no
PatternRegistry, no services), because a historical migration must keep working
after those change.

Non-destructive: every added column is nullable or server-defaulted with a
conservative value, so installing this revision cannot change any existing
event's visibility or any policy's outcome.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e3c9f21a7b48"
down_revision: str | None = "d7a1c4e83b60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("events", sa.Column("occurrence_id", sa.String(length=512), nullable=True))
    op.add_column(
        "events", sa.Column("recurrence_parent_id", sa.String(length=512), nullable=True)
    )
    op.add_column(
        "events",
        sa.Column(
            "is_recurrence_parent", sa.Boolean(), nullable=False, server_default=sa.text("0")
        ),
    )
    op.add_column(
        "events",
        sa.Column("is_cancelled", sa.Boolean(), nullable=False, server_default=sa.text("0")),
    )
    op.create_index("ix_events_occurrence_id", "events", ["occurrence_id"], unique=False)
    op.create_index(
        "ix_events_recurrence_parent_id", "events", ["recurrence_parent_id"], unique=False
    )

    op.add_column(
        "auto_onboarding_policies",
        sa.Column(
            "require_date_range_parse_success",
            sa.Boolean(), nullable=False, server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "auto_onboarding_policies",
        sa.Column(
            "minimum_date_range_parse_success",
            sa.Float(), nullable=False, server_default=sa.text("0.95"),
        ),
    )
    op.add_column(
        "auto_onboarding_policies",
        sa.Column(
            "require_geographic_filter",
            sa.Boolean(), nullable=False, server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "auto_onboarding_policies",
        sa.Column(
            "minimum_geographic_inclusion_rate",
            sa.Float(), nullable=False, server_default=sa.text("0.0"),
        ),
    )


def downgrade() -> None:
    op.drop_column("auto_onboarding_policies", "minimum_geographic_inclusion_rate")
    op.drop_column("auto_onboarding_policies", "require_geographic_filter")
    op.drop_column("auto_onboarding_policies", "minimum_date_range_parse_success")
    op.drop_column("auto_onboarding_policies", "require_date_range_parse_success")
    op.drop_index("ix_events_recurrence_parent_id", table_name="events")
    op.drop_index("ix_events_occurrence_id", table_name="events")
    op.drop_column("events", "is_cancelled")
    op.drop_column("events", "is_recurrence_parent")
    op.drop_column("events", "recurrence_parent_id")
    op.drop_column("events", "occurrence_id")
