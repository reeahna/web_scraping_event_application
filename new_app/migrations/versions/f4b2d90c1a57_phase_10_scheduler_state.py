"""phase 10 durable scheduler state

Revision ID: f4b2d90c1a57
Revises: e3c9f21a7b48

Adds the two tables behind durable scheduling:

* ``scheduler_job_state`` — one row per scheduled website: the per-site lock
  (running + holder + heartbeat), the durable schedule (next/last run, status),
  a cancellation request, a scheduler-level pause, and a structural-failure
  counter driving re-onboarding.
* ``scheduler_leader`` — a single advisory row so exactly one scheduler process
  acts even if two are started.

Self-contained: imports nothing from the application. Additive and
non-destructive — both tables are new, and installing this revision cannot
change any existing row or start any scheduling on its own (a dedicated
scheduler process must be run for that).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f4b2d90c1a57"
down_revision: str | None = "e3c9f21a7b48"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "scheduler_job_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "website_id",
            sa.Integer(),
            sa.ForeignKey("websites.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("paused", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_status", sa.String(length=32), nullable=True),
        sa.Column("running", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("lock_holder", sa.String(length=128), nullable=True),
        sa.Column("lock_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("run_correlation_id", sa.String(length=64), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "consecutive_structure_failures", sa.Integer(), nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("website_id", name="uq_scheduler_job_state_website"),
    )
    op.create_index(
        "ix_scheduler_job_state_website_id", "scheduler_job_state", ["website_id"], unique=False
    )

    op.create_table(
        "scheduler_leader",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("holder", sa.String(length=128), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("scheduler_leader")
    op.drop_index("ix_scheduler_job_state_website_id", table_name="scheduler_job_state")
    op.drop_table("scheduler_job_state")
