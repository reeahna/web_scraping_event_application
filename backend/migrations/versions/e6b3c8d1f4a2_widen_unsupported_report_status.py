"""widen unsupported_site_reports.status to fit every report status

"waiting_for_browser_support" is 27 characters; the column was VARCHAR(16).
SQLite ignored the limit, PostgreSQL rejects the update.

Revision ID: e6b3c8d1f4a2
Revises: d2a8f5c3e9b1
"""

import sqlalchemy as sa
from alembic import op

revision = "e6b3c8d1f4a2"
down_revision = "d2a8f5c3e9b1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("unsupported_site_reports") as batch:
        batch.alter_column(
            "status", existing_type=sa.String(16), type_=sa.String(32), existing_nullable=False
        )


def downgrade() -> None:
    with op.batch_alter_table("unsupported_site_reports") as batch:
        batch.alter_column(
            "status", existing_type=sa.String(32), type_=sa.String(16), existing_nullable=False
        )
