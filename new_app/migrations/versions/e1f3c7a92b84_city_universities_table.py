"""city_universities

A town can have more than one school, so the single university_name column
becomes a row per school. Existing values are carried across before the column
is dropped, so no town loses the school it already had.

Revision ID: e1f3c7a92b84
Revises: d7e4b1c9a250
"""

import sqlalchemy as sa
from alembic import op

revision = "e1f3c7a92b84"
down_revision = "d7e4b1c9a250"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "city_universities",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "city_id",
            sa.Integer(),
            sa.ForeignKey("cities.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.UniqueConstraint("city_id", "name", name="uq_city_university_name"),
    )

    # Carry the existing single value across first, so dropping the column
    # below cannot lose data.
    op.execute(
        "INSERT INTO city_universities (city_id, name) "
        "SELECT id, university_name FROM cities "
        "WHERE university_name IS NOT NULL AND TRIM(university_name) != ''"
    )

    # batch mode: SQLite cannot drop a column in place.
    with op.batch_alter_table("cities") as batch:
        batch.drop_column("university_name")


def downgrade() -> None:
    with op.batch_alter_table("cities") as batch:
        batch.add_column(sa.Column("university_name", sa.String(length=255), nullable=True))

    # Restore one school per town; a town with several keeps the first by name,
    # which is the most the old single column could hold.
    op.execute(
        "UPDATE cities SET university_name = ("
        "  SELECT name FROM city_universities"
        "  WHERE city_universities.city_id = cities.id"
        "  ORDER BY name LIMIT 1"
        ")"
    )
    op.drop_table("city_universities")
