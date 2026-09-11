"""category photo placeholder pool

Revision ID: b2d9f1a7c4e5
Revises: a1c7e5f30b92

Adds ``category_photos`` — a pool of Unsplash photos per event category, used as
a relevant placeholder image for events that carry no image of their own. Each
row stores the photographer credit Unsplash requires when a photo is shown.

Self-contained and additive: the table is new, starts empty (populated by
scripts/fetch_category_photos.py), and changes no existing row.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b2d9f1a7c4e5"
down_revision: str | None = "a1c7e5f30b92"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "category_photos",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("category_slug", sa.String(length=100), nullable=False),
        sa.Column("unsplash_id", sa.String(length=100), nullable=False),
        sa.Column("image_url", sa.String(length=1000), nullable=False),
        sa.Column("thumb_url", sa.String(length=1000), nullable=True),
        sa.Column("photographer", sa.String(length=200), nullable=False),
        sa.Column("photographer_url", sa.String(length=500), nullable=False),
        sa.Column("source_url", sa.String(length=500), nullable=False),
        # Defined inline (not via ALTER) so it works on SQLite.
        sa.UniqueConstraint("unsplash_id", name="uq_category_photos_unsplash_id"),
    )
    op.create_index("ix_category_photos_category_slug", "category_photos", ["category_slug"])


def downgrade() -> None:
    op.drop_index("ix_category_photos_category_slug", table_name="category_photos")
    op.drop_table("category_photos")
