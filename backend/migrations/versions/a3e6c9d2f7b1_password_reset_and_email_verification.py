"""password reset and email verification

Adds user_tokens (hashed one-time links for password reset and address
verification) and users.email_verified_at. Accounts that signed in through a
provider that verified their email are marked verified from that record.

Revision ID: a3e6c9d2f7b1
Revises: f2a8d4c61e37
"""

import sqlalchemy as sa
from alembic import op

revision = "a3e6c9d2f7b1"
down_revision = "f2a8d4c61e37"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_tokens",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_user_tokens_user_id", "user_tokens", ["user_id"])

    with op.batch_alter_table("users") as batch:
        batch.add_column(
            sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True)
        )

    # An address a sign-in provider has already verified is verified here too.
    users = sa.table("users", sa.column("id"), sa.column("email_verified_at"))
    identities = sa.table(
        "external_identities", sa.column("user_id"), sa.column("email_verified", sa.Boolean)
    )
    op.execute(
        users.update()
        .where(
            users.c.id.in_(
                sa.select(identities.c.user_id).where(
                    identities.c.email_verified.is_(sa.true())
                )
            )
        )
        .values(email_verified_at=sa.func.now())
    )


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_column("email_verified_at")
    op.drop_index("ix_user_tokens_user_id", table_name="user_tokens")
    op.drop_table("user_tokens")
