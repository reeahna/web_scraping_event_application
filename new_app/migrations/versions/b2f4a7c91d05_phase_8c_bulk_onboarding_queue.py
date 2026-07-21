"""phase 8c bulk onboarding queue

Revision ID: b2f4a7c91d05
Revises: 7541eeff3182

Adds `onboarding_batches` and `onboarding_jobs` — the persisted queue behind
bulk source onboarding. Both are new tables with no seed data, and no
existing table or row is touched, so upgrading and downgrading are both
non-destructive to pre-existing data. Downgrade drops only these two tables,
which means it discards onboarding history; the websites those jobs created
are unaffected, because the job -> website link lives on the job side.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b2f4a7c91d05"
down_revision: str | None = "7541eeff3182"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "onboarding_batches",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("submitted_by_user_id", sa.Integer(), nullable=True),
        sa.Column("default_city_id", sa.Integer(), nullable=True),
        sa.Column("default_timezone", sa.String(length=64), nullable=True),
        sa.Column(
            "redetect_existing", sa.Boolean(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "source_kind", sa.String(length=16), nullable=False, server_default="paste"
        ),
        sa.Column("submitted_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("valid_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("invalid_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="open"),
        sa.Column("rejected_rows", sa.JSON(), nullable=True),
        sa.Column("correlation_id", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["submitted_by_user_id"],
            ["users.id"],
            name="fk_onboarding_batches_submitted_by_user",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["default_city_id"],
            ["cities.id"],
            name="fk_onboarding_batches_default_city",
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_onboarding_batches_status", "onboarding_batches", ["status"], unique=False
    )
    op.create_index(
        "ix_onboarding_batches_correlation_id",
        "onboarding_batches",
        ["correlation_id"],
        unique=False,
    )

    op.create_table(
        "onboarding_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("batch_id", sa.Integer(), nullable=True),
        sa.Column("row_number", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("submitted_url", sa.String(length=2000), nullable=False),
        sa.Column("normalized_url", sa.String(length=2000), nullable=False),
        sa.Column("final_url", sa.String(length=2000), nullable=True),
        sa.Column("city_id", sa.Integer(), nullable=True),
        sa.Column("timezone_override", sa.String(length=64), nullable=True),
        sa.Column("submitted_by_user_id", sa.Integer(), nullable=True),
        sa.Column("submitted_name", sa.String(length=255), nullable=True),
        sa.Column("submitted_source_display_name", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
        sa.Column("current_step", sa.String(length=32), nullable=True),
        sa.Column("website_id", sa.Integer(), nullable=True),
        sa.Column("duplicate_of_website_id", sa.Integer(), nullable=True),
        sa.Column("detected_pattern", sa.String(length=64), nullable=True),
        sa.Column("detection_confidence", sa.Float(), nullable=True),
        sa.Column("detection_run_id", sa.Integer(), nullable=True),
        sa.Column("preview_run_id", sa.Integer(), nullable=True),
        sa.Column("configuration_version", sa.Integer(), nullable=True),
        sa.Column("events_found", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("events_valid", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("events_rejected", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("quality", sa.JSON(), nullable=True),
        sa.Column("inferred_metadata", sa.JSON(), nullable=True),
        sa.Column("failure_reason", sa.String(length=500), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("correlation_id", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["onboarding_batches.id"],
            name="fk_onboarding_jobs_batch",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["city_id"], ["cities.id"], name="fk_onboarding_jobs_city", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["submitted_by_user_id"],
            ["users.id"],
            name="fk_onboarding_jobs_submitted_by_user",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["website_id"],
            ["websites.id"],
            name="fk_onboarding_jobs_website",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["duplicate_of_website_id"],
            ["websites.id"],
            name="fk_onboarding_jobs_duplicate_website",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["detection_run_id"],
            ["extraction_runs.id"],
            name="fk_onboarding_jobs_detection_run",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["preview_run_id"],
            ["extraction_runs.id"],
            name="fk_onboarding_jobs_preview_run",
            ondelete="SET NULL",
        ),
    )
    op.create_index("ix_onboarding_jobs_batch_id", "onboarding_jobs", ["batch_id"], unique=False)
    op.create_index(
        "ix_onboarding_jobs_normalized_url", "onboarding_jobs", ["normalized_url"], unique=False
    )
    op.create_index("ix_onboarding_jobs_status", "onboarding_jobs", ["status"], unique=False)
    op.create_index("ix_onboarding_jobs_website_id", "onboarding_jobs", ["website_id"], unique=False)
    op.create_index(
        "ix_onboarding_jobs_correlation_id", "onboarding_jobs", ["correlation_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_onboarding_jobs_correlation_id", table_name="onboarding_jobs")
    op.drop_index("ix_onboarding_jobs_website_id", table_name="onboarding_jobs")
    op.drop_index("ix_onboarding_jobs_status", table_name="onboarding_jobs")
    op.drop_index("ix_onboarding_jobs_normalized_url", table_name="onboarding_jobs")
    op.drop_index("ix_onboarding_jobs_batch_id", table_name="onboarding_jobs")
    op.drop_table("onboarding_jobs")

    op.drop_index("ix_onboarding_batches_correlation_id", table_name="onboarding_batches")
    op.drop_index("ix_onboarding_batches_status", table_name="onboarding_batches")
    op.drop_table("onboarding_batches")
