"""preview run quality

Revision ID: a6d3f1c8e2b4
Revises: e2b6f4a8c3d1

Adds extraction_runs.quality: the scored preview quality of a preview run, so
the admin pages can show the latest preview's quality instead of the snapshot
taken when the source was first onboarded.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a6d3f1c8e2b4"
down_revision: str | None = "e2b6f4a8c3d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("extraction_runs") as batch:
        batch.add_column(sa.Column("quality", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("extraction_runs") as batch:
        batch.drop_column("quality")
