"""ai label tracking

Revision ID: d7a4c2e9b1f3
Revises: c3d8e1f5a9b2

Adds per-event AI label bookkeeping (status, attempts, last attempt and error,
a digest of the words the model read) and when an administrator last set the
event's visibility by hand, plus the ai_label_batches table that records each
labeling request's token use and rate-limit answer.

Existing rows get NULL status and zero attempts, which the labeling queue reads
exactly as it read them before: AI-labeled events stay labeled and everything
else stays waiting.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d7a4c2e9b1f3"
down_revision: str | None = "c3d8e1f5a9b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("events") as batch:
        batch.add_column(sa.Column("ai_label_status", sa.String(length=16)))
        batch.add_column(
            sa.Column("ai_attempts", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(sa.Column("ai_last_attempt_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("ai_last_error", sa.String(length=300)))
        batch.add_column(sa.Column("ai_input_hash", sa.String(length=64)))
        batch.add_column(sa.Column("visibility_set_by_admin_at", sa.DateTime(timezone=True)))
        batch.create_index("ix_events_ai_label_status", ["ai_label_status"])

    op.create_table(
        "ai_label_batches",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("http_status", sa.Integer()),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column("events_submitted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("events_labeled", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("events_skipped", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("events_failed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("events_hidden", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("prompt_tokens", sa.Integer()),
        sa.Column("completion_tokens", sa.Integer()),
        sa.Column("total_tokens", sa.Integer()),
        sa.Column("cached_tokens", sa.Integer()),
        sa.Column("reasoning_tokens", sa.Integer()),
        sa.Column("limit_requests", sa.Integer()),
        sa.Column("remaining_requests", sa.Integer()),
        sa.Column("reset_requests", sa.String(length=32)),
        sa.Column("limit_tokens", sa.Integer()),
        sa.Column("remaining_tokens", sa.Integer()),
        sa.Column("reset_tokens", sa.String(length=32)),
        sa.Column("retry_after_seconds", sa.Float()),
        sa.Column("limit_kind", sa.String(length=8)),
        sa.Column("limit_value", sa.Integer()),
        sa.Column("limit_used", sa.Integer()),
        sa.Column("limit_requested", sa.Integer()),
        sa.Column("error", sa.String(length=300)),
    )
    op.create_index("ix_ai_label_batches_created_at", "ai_label_batches", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_ai_label_batches_created_at", table_name="ai_label_batches")
    op.drop_table("ai_label_batches")
    with op.batch_alter_table("events") as batch:
        batch.drop_index("ix_events_ai_label_status")
        batch.drop_column("visibility_set_by_admin_at")
        batch.drop_column("ai_input_hash")
        batch.drop_column("ai_last_error")
        batch.drop_column("ai_last_attempt_at")
        batch.drop_column("ai_attempts")
        batch.drop_column("ai_label_status")
