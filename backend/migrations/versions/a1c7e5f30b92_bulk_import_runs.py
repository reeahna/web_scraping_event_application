"""bulk manual import runs

Revision ID: a1c7e5f30b92
Revises: f9a2c1d4e6b8

Adds tracking for an administrator's "import all active websites" operation:

* ``bulk_import_runs`` — one header row per bulk operation (who, when, scoped
  counts, overall status).
* ``bulk_import_items`` — one row per website in a bulk run, with its execution
  strategy, per-site status, event counts, and a link to the ordinary
  ExtractionRun it produced.

Self-contained and additive: both tables are new and installing this revision
changes no existing row and starts no import on its own.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1c7e5f30b92"
down_revision: str | None = "f9a2c1d4e6b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "bulk_import_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "requested_by_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
        sa.Column("eligible_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("http_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("browser_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("skipped_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "already_running_count", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
    )
    op.create_index("ix_bulk_import_runs_status", "bulk_import_runs", ["status"], unique=False)

    op.create_table(
        "bulk_import_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "bulk_run_id",
            sa.Integer(),
            sa.ForeignKey("bulk_import_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "website_id",
            sa.Integer(),
            sa.ForeignKey("websites.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("website_name", sa.String(length=255), nullable=False),
        sa.Column(
            "execution_strategy", sa.String(length=16), nullable=False, server_default="http"
        ),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
        sa.Column("events_found", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("events_valid", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("events_inserted", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("events_updated", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("duplicates_skipped", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "extraction_run_id",
            sa.Integer(),
            sa.ForeignKey("extraction_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("error_summary", sa.String(length=500), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_bulk_import_items_bulk_run_id", "bulk_import_items", ["bulk_run_id"], unique=False
    )
    op.create_index(
        "ix_bulk_import_items_website_id", "bulk_import_items", ["website_id"], unique=False
    )
    op.create_index("ix_bulk_import_items_status", "bulk_import_items", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_bulk_import_items_status", table_name="bulk_import_items")
    op.drop_index("ix_bulk_import_items_website_id", table_name="bulk_import_items")
    op.drop_index("ix_bulk_import_items_bulk_run_id", table_name="bulk_import_items")
    op.drop_table("bulk_import_items")
    op.drop_index("ix_bulk_import_runs_status", table_name="bulk_import_runs")
    op.drop_table("bulk_import_runs")
