"""city.university_name

This product covers college cities, so each city carries the school it is
known for. Nullable: a city without one still works, it simply shows its state
instead in the chooser.

Revision ID: d7e4b1c9a250
Revises: c4a7e2b91d63
"""

import sqlalchemy as sa
from alembic import op

revision = "d7e4b1c9a250"
down_revision = "c4a7e2b91d63"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("cities", sa.Column("university_name", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("cities", "university_name")
