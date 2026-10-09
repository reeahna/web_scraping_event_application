"""phase 6 extraction engine core

Revision ID: 5922ed0a82de
Revises: f5c3d9a1e7b2

Purely additive schema changes: four new tables plus four new nullable/
defaulted columns on `websites`. Nothing here alters existing row *data* on
`events`/`websites`/`cities`/etc. — every field the extraction engine needs
on `events` already exists from Phase 5, and no data seeding happens in this
migration (unlike Phase 5's category seed), so upgrade/downgrade are pure
schema operations with no live-application-code dependency.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5922ed0a82de"
down_revision: str | None = "f5c3d9a1e7b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("websites") as batch_op:
        batch_op.add_column(sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("approved_by_user_id", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column("configuration_version", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(sa.Column("active_configuration_version", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_websites_approved_by_user",
            "users",
            ["approved_by_user_id"],
            ["id"],
            ondelete="SET NULL",
        )

    op.create_table(
        "extraction_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("website_id", sa.Integer(), nullable=False),
        sa.Column("configuration_version", sa.Integer(), nullable=True),
        sa.Column("pattern_name", sa.String(64), nullable=True),
        sa.Column("run_type", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("source_url", sa.String(2000), nullable=False),
        sa.Column("final_url", sa.String(2000), nullable=True),
        sa.Column("events_found", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("events_valid", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("events_rejected", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("events_inserted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("events_updated", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duplicates_skipped", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("warnings", sa.JSON(), nullable=True),
        sa.Column("error_summary", sa.String(1000), nullable=True),
        sa.Column("detector_evidence", sa.JSON(), nullable=True),
        sa.Column("response_metadata", sa.JSON(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("initiating_user_id", sa.Integer(), nullable=True),
        sa.Column("correlation_id", sa.String(64), nullable=True),
        sa.ForeignKeyConstraint(["website_id"], ["websites.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["initiating_user_id"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_extraction_runs_website_id", "extraction_runs", ["website_id"])
    op.create_index("ix_extraction_runs_status", "extraction_runs", ["status"])
    op.create_index("ix_extraction_runs_correlation_id", "extraction_runs", ["correlation_id"])

    op.create_table(
        "extraction_errors",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("extraction_run_id", sa.Integer(), nullable=False),
        sa.Column("stage", sa.String(32), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=False),
        sa.Column("safe_message", sa.String(500), nullable=False),
        sa.Column("candidate_index", sa.Integer(), nullable=True),
        sa.Column("field_name", sa.String(255), nullable=True),
        sa.Column("source_page", sa.String(2000), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["extraction_run_id"], ["extraction_runs.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "ix_extraction_errors_extraction_run_id", "extraction_errors", ["extraction_run_id"]
    )
    op.create_index("ix_extraction_errors_stage", "extraction_errors", ["stage"])
    op.create_index("ix_extraction_errors_error_code", "extraction_errors", ["error_code"])

    op.create_table(
        "event_provenance",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("extraction_run_id", sa.Integer(), nullable=True),
        sa.Column("website_id", sa.Integer(), nullable=False),
        sa.Column("source_page", sa.String(2000), nullable=False),
        sa.Column("extraction_pattern", sa.String(64), nullable=False),
        sa.Column("pattern_version", sa.String(32), nullable=False),
        sa.Column("raw_record_hash", sa.String(64), nullable=False),
        sa.Column("source_response_hash", sa.String(64), nullable=False),
        sa.Column("field_source_paths", sa.JSON(), nullable=True),
        sa.Column("transformation_history", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["extraction_run_id"], ["extraction_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["website_id"], ["websites.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("event_id", "extraction_run_id", name="uq_event_provenance_run"),
    )
    op.create_index("ix_event_provenance_event_id", "event_provenance", ["event_id"])
    op.create_index(
        "ix_event_provenance_extraction_run_id", "event_provenance", ["extraction_run_id"]
    )
    op.create_index("ix_event_provenance_website_id", "event_provenance", ["website_id"])

    op.create_table(
        "unsupported_site_reports",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("website_id", sa.Integer(), nullable=False),
        sa.Column("submitted_url", sa.String(2000), nullable=False),
        sa.Column("final_url", sa.String(2000), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("page_title", sa.String(500), nullable=True),
        sa.Column("detected_platform_evidence", sa.JSON(), nullable=True),
        sa.Column("available_detector_results", sa.JSON(), nullable=True),
        sa.Column("discovered_endpoints", sa.JSON(), nullable=True),
        sa.Column("browser_required", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("json_ld_presence", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("pagination_indicators", sa.JSON(), nullable=True),
        sa.Column(
            "access_denied_or_challenge_detected",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("failure_reason", sa.String(500), nullable=True),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["website_id"], ["websites.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "ix_unsupported_site_reports_website_id", "unsupported_site_reports", ["website_id"]
    )
    op.create_index(
        "ix_unsupported_site_reports_fingerprint", "unsupported_site_reports", ["fingerprint"]
    )


def downgrade() -> None:
    op.drop_index("ix_unsupported_site_reports_fingerprint", table_name="unsupported_site_reports")
    op.drop_index("ix_unsupported_site_reports_website_id", table_name="unsupported_site_reports")
    op.drop_table("unsupported_site_reports")

    op.drop_index("ix_event_provenance_website_id", table_name="event_provenance")
    op.drop_index("ix_event_provenance_extraction_run_id", table_name="event_provenance")
    op.drop_index("ix_event_provenance_event_id", table_name="event_provenance")
    op.drop_table("event_provenance")

    op.drop_index("ix_extraction_errors_error_code", table_name="extraction_errors")
    op.drop_index("ix_extraction_errors_stage", table_name="extraction_errors")
    op.drop_index("ix_extraction_errors_extraction_run_id", table_name="extraction_errors")
    op.drop_table("extraction_errors")

    op.drop_index("ix_extraction_runs_correlation_id", table_name="extraction_runs")
    op.drop_index("ix_extraction_runs_status", table_name="extraction_runs")
    op.drop_index("ix_extraction_runs_website_id", table_name="extraction_runs")
    op.drop_table("extraction_runs")

    with op.batch_alter_table("websites") as batch_op:
        batch_op.drop_constraint("fk_websites_approved_by_user", type_="foreignkey")
        batch_op.drop_column("active_configuration_version")
        batch_op.drop_column("configuration_version")
        batch_op.drop_column("approved_by_user_id")
        batch_op.drop_column("approved_at")
