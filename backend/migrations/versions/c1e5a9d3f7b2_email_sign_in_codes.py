"""email sign-in codes and sign-in history

Revision ID: c1e5a9d3f7b2
Revises: b8e2d4a6c1f3

Adds two tables for signing in with a code sent by email:
email_sign_in_codes (one row per code sent, holding an HMAC of the code keyed
with a server secret that is not in the database, its expiry, wrong-guess
count and used/invalidated state) and sign_in_history
(codes sent and rejected, and every successful sign-in by any method). Both
start empty; no existing row changes.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c1e5a9d3f7b2"
down_revision: str | None = "b8e2d4a6c1f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "email_sign_in_codes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("code_hash", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("failed_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("requested_ip", sa.String(64), nullable=True),
    )
    op.create_index(
        "ix_email_sign_in_codes_email", "email_sign_in_codes", ["email"]
    )

    op.create_table(
        "sign_in_history",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column("method", sa.String(32), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("detail", sa.String(255), nullable=True),
        sa.Column("ip_address", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_sign_in_history_user_id", "sign_in_history", ["user_id"])
    op.create_index("ix_sign_in_history_email", "sign_in_history", ["email"])
    op.create_index("ix_sign_in_history_created_at", "sign_in_history", ["created_at"])
    op.create_index("ix_sign_in_history_ip_address", "sign_in_history", ["ip_address"])


def downgrade() -> None:
    op.drop_index("ix_sign_in_history_ip_address", table_name="sign_in_history")
    op.drop_index("ix_sign_in_history_created_at", table_name="sign_in_history")
    op.drop_index("ix_sign_in_history_email", table_name="sign_in_history")
    op.drop_index("ix_sign_in_history_user_id", table_name="sign_in_history")
    op.drop_table("sign_in_history")
    op.drop_index("ix_email_sign_in_codes_email", table_name="email_sign_in_codes")
    op.drop_table("email_sign_in_codes")
