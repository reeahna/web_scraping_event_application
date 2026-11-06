"""town_onboarding_requests

A college town an administrator asked Claude to onboard: the scheduler finds
its event sources, creates the city and queues the sources for onboarding.

Revision ID: a3e7c2d95b14
Revises: e6b3c8d1f4a2
"""

import sqlalchemy as sa
from alembic import op

revision = "a3e7c2d95b14"
down_revision = "e6b3c8d1f4a2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "town_onboarding_requests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("town_name", sa.String(length=255), nullable=False),
        sa.Column("state_or_region", sa.String(length=100), nullable=True),
        sa.Column("universities", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("failure_reason", sa.String(length=500), nullable=True),
        sa.Column("proposal", sa.JSON(), nullable=True),
        sa.Column(
            "city_id", sa.Integer(), sa.ForeignKey("cities.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "batch_id", sa.Integer(),
            sa.ForeignKey("onboarding_batches.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column(
            "requested_by_user_id", sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_town_onboarding_requests_status", "town_onboarding_requests", ["status"]
    )


def downgrade() -> None:
    op.drop_index("ix_town_onboarding_requests_status", table_name="town_onboarding_requests")
    op.drop_table("town_onboarding_requests")
