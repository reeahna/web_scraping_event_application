"""unsupported report browser recovery evidence

Revision ID: f9a2c1d4e6b8
Revises: e8b3f2c05a19

Adds unsupported_site_reports.browser_recovery: a nullable JSON column holding
a bounded, redacted summary of the most recent restricted-browser recovery
attempt (attempted_at, status, observed response types, discovered endpoints,
chosen source, proposed pattern, preview outcome, blocked reason, safe error
summary). Additive and nullable; changes no existing row's behaviour and stores
no cookies, headers, credentials, tokens, or full response bodies.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f9a2c1d4e6b8"
down_revision: str | None = "e8b3f2c05a19"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "unsupported_site_reports",
        sa.Column("browser_recovery", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("unsupported_site_reports", "browser_recovery")
