"""phase 7 onboarding workflow

Revision ID: 7541eeff3182
Revises: 5922ed0a82de

Adds unsupported-site-report lifecycle columns (assignment, notes,
occurrence tracking, resolution) and a new `notifications` table. No
existing row *data* is altered — every added column is nullable or
defaulted, and `notifications` is a new table with no seed data. Also seeds
the new `reports.manage` permission (idempotent, same pattern as
84dd6dda79ec's `seed_defaults()` call) so upgrading grants it to
Administrator/Super Administrator without a manual bootstrap step.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.orm import Session

from app.core.seed import seed_defaults

revision: str = "7541eeff3182"
down_revision: str | None = "5922ed0a82de"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NEW_PERMISSION_CODE = "reports.manage"


def upgrade() -> None:
    with op.batch_alter_table("unsupported_site_reports") as batch_op:
        batch_op.add_column(sa.Column("assigned_user_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("admin_notes", sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "last_seen_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            )
        )
        batch_op.add_column(
            sa.Column("occurrence_count", sa.Integer(), nullable=False, server_default="1")
        )
        batch_op.add_column(sa.Column("latest_extraction_run_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("resolved_by_user_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_unsupported_site_reports_assigned_user",
            "users",
            ["assigned_user_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_foreign_key(
            "fk_unsupported_site_reports_resolved_by_user",
            "users",
            ["resolved_by_user_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_foreign_key(
            "fk_unsupported_site_reports_latest_extraction_run",
            "extraction_runs",
            ["latest_extraction_run_id"],
            ["id"],
            ondelete="SET NULL",
        )

    op.create_table(
        "notifications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("recipient_user_id", sa.Integer(), nullable=False),
        sa.Column("notification_type", sa.String(64), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("message", sa.String(1000), nullable=False),
        sa.Column("related_resource_type", sa.String(32), nullable=True),
        sa.Column("related_resource_id", sa.Integer(), nullable=True),
        sa.Column("action_url", sa.String(500), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "delivery_status", sa.String(16), nullable=False, server_default="not_applicable"
        ),
        sa.Column("delivery_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("provider", sa.String(32), nullable=True),
        sa.Column("dedup_fingerprint", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(["recipient_user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_notifications_recipient_user_id", "notifications", ["recipient_user_id"])
    op.create_index("ix_notifications_notification_type", "notifications", ["notification_type"])
    op.create_index("ix_notifications_dedup_fingerprint", "notifications", ["dedup_fingerprint"])
    op.create_index(
        "ix_notifications_recipient_read", "notifications", ["recipient_user_id", "read_at"]
    )

    bind = op.get_bind()
    session = Session(bind=bind)
    seed_defaults(session)
    session.close()


def downgrade() -> None:
    bind = op.get_bind()
    session = Session(bind=bind)
    session.execute(
        sa.text(
            "DELETE FROM role_permissions WHERE permission_id IN "
            "(SELECT id FROM permissions WHERE code = :code)"
        ),
        {"code": _NEW_PERMISSION_CODE},
    )
    session.execute(
        sa.text("DELETE FROM permissions WHERE code = :code"), {"code": _NEW_PERMISSION_CODE}
    )
    session.commit()
    session.close()

    op.drop_index("ix_notifications_recipient_read", table_name="notifications")
    op.drop_index("ix_notifications_dedup_fingerprint", table_name="notifications")
    op.drop_index("ix_notifications_notification_type", table_name="notifications")
    op.drop_index("ix_notifications_recipient_user_id", table_name="notifications")
    op.drop_table("notifications")

    with op.batch_alter_table("unsupported_site_reports") as batch_op:
        batch_op.drop_constraint(
            "fk_unsupported_site_reports_latest_extraction_run", type_="foreignkey"
        )
        batch_op.drop_constraint("fk_unsupported_site_reports_resolved_by_user", type_="foreignkey")
        batch_op.drop_constraint("fk_unsupported_site_reports_assigned_user", type_="foreignkey")
        batch_op.drop_column("resolved_by_user_id")
        batch_op.drop_column("resolved_at")
        batch_op.drop_column("latest_extraction_run_id")
        batch_op.drop_column("occurrence_count")
        batch_op.drop_column("last_seen_at")
        batch_op.drop_column("admin_notes")
        batch_op.drop_column("assigned_user_id")
