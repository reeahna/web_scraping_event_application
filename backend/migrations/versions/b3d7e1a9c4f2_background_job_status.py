"""background job status: last run of each scheduler background job

Revision ID: b3d7e1a9c4f2
Revises: a7c4e2f9b318
"""

import sqlalchemy as sa
from alembic import op

revision = "b3d7e1a9c4f2"
down_revision = "a7c4e2f9b318"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "background_job_status",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("job", sa.String(64), nullable=False),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_status", sa.String(16), nullable=True),
        sa.Column("last_result", sa.String(255), nullable=True),
        sa.Column("last_error", sa.String(1000), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("paused_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("job", name="uq_background_job_status_job"),
    )


def downgrade() -> None:
    op.drop_table("background_job_status")
