"""phase 14 external identities and oauth state

Revision ID: c9d4a1b6e072
Revises: b7e2f5a4c318

Adds external_identities (the provider-independent link between a local user
and an external account; unique on (provider, subject) to prevent duplicate
identities) and oauth_login_states (short-lived server-side CSRF state + OIDC
nonce for an in-flight login).

Self-contained and additive — both tables are new; no third-party password or
provider token is stored. Installing this revision changes nothing existing and
enables no provider on its own (providers require configured credentials).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c9d4a1b6e072"
down_revision: str | None = "b7e2f5a4c318"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "external_identities",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("email_verified", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("display_name", sa.String(length=255), nullable=True),
        sa.Column("avatar_url", sa.String(length=2000), nullable=True),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("provider", "subject", name="uq_external_identity_provider_subject"),
    )
    op.create_index(
        "ix_external_identities_user_id", "external_identities", ["user_id"], unique=False
    )
    op.create_index(
        "ix_external_identities_provider", "external_identities", ["provider"], unique=False
    )
    op.create_index(
        "ix_external_identities_subject", "external_identities", ["subject"], unique=False
    )

    op.create_table(
        "oauth_login_states",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("state", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("nonce", sa.String(length=64), nullable=True),
        sa.Column("next_url", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_oauth_login_states_state", "oauth_login_states", ["state"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_oauth_login_states_state", table_name="oauth_login_states")
    op.drop_table("oauth_login_states")
    op.drop_index("ix_external_identities_subject", table_name="external_identities")
    op.drop_index("ix_external_identities_provider", table_name="external_identities")
    op.drop_index("ix_external_identities_user_id", table_name="external_identities")
    op.drop_table("external_identities")
