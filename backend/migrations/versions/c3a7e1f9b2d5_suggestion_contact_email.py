"""suggestion contact email

Revision ID: c3a7e1f9b2d5
Revises: b8e2d4a6c1f3

Adds suggestions.contact_email: the optional address a visitor who is not
signed in leaves on the "New features / bug fixes" page so the team can reply.
Nullable and empty for every existing row, so nothing already stored changes.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3a7e1f9b2d5"
down_revision: str | None = "b8e2d4a6c1f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("suggestions") as batch:
        batch.add_column(sa.Column("contact_email", sa.String(320), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("suggestions") as batch:
        batch.drop_column("contact_email")
