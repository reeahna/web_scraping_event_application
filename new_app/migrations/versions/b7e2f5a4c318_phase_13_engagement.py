"""phase 13 saved events, follows, and alerts

Revision ID: b7e2f5a4c318
Revises: a1c8e6f4920d

Adds the registered-user engagement tables: saved_events, user_follows,
alert_preferences, and alert_deliveries. Self-contained and additive — all
four tables are new and owned per-user; installing this revision changes no
existing row and grants no administrative capability.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7e2f5a4c318"
down_revision: str | None = "a1c8e6f4920d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "saved_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"),
            nullable=False,
        ),
        *_timestamps(),
        sa.UniqueConstraint("user_id", "event_id", name="uq_saved_event_user_event"),
    )
    op.create_index("ix_saved_events_user_id", "saved_events", ["user_id"], unique=False)
    op.create_index("ix_saved_events_event_id", "saved_events", ["event_id"], unique=False)

    op.create_table(
        "user_follows",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("follow_type", sa.String(length=16), nullable=False),
        sa.Column("target_id", sa.Integer(), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint("user_id", "follow_type", "target_id", name="uq_user_follow"),
    )
    op.create_index("ix_user_follows_user_id", "user_follows", ["user_id"], unique=False)
    op.create_index("ix_user_follows_follow_type", "user_follows", ["follow_type"], unique=False)
    op.create_index("ix_user_follows_target_id", "user_follows", ["target_id"], unique=False)

    op.create_table(
        "alert_preferences",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("in_app_enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("email_enabled", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "frequency", sa.String(length=16), nullable=False, server_default=sa.text("'immediate'")
        ),
        sa.Column("notify_new_events", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("notify_reminders", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("notify_updates", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("unsubscribe_token", sa.String(length=64), nullable=False),
        sa.Column("last_digest_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint("user_id", name="uq_alert_preference_user"),
    )
    op.create_index(
        "ix_alert_preferences_user_id", "alert_preferences", ["user_id"], unique=False
    )
    op.create_index(
        "ix_alert_preferences_unsubscribe_token", "alert_preferences",
        ["unsubscribe_token"], unique=True,
    )

    op.create_table(
        "alert_deliveries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("alert_key", sa.String(length=200), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default=sa.text("'sent'")),
        sa.Column(
            "event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("body", sa.String(length=1000), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint("user_id", "alert_key", "channel", name="uq_alert_delivery"),
    )
    op.create_index("ix_alert_deliveries_user_id", "alert_deliveries", ["user_id"], unique=False)
    op.create_index(
        "ix_alert_deliveries_alert_key", "alert_deliveries", ["alert_key"], unique=False
    )


def downgrade() -> None:
    op.drop_table("alert_deliveries")
    op.drop_table("alert_preferences")
    op.drop_table("user_follows")
    op.drop_table("saved_events")
