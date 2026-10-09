"""social sign-in only: remove passwords

Sign-in is now only through Google, Microsoft or Facebook. This:

1. Deletes every account that has no linked sign-in provider, i.e. every
   password account ("start fresh"). Rows that point at a deleted user follow
   each foreign key's own rule, applied here explicitly: CASCADE rows are
   deleted, the rest are set to NULL. (SQLite may run migrations with foreign
   key enforcement off, so the database cannot be relied on to do it.)
2. Drops users.hashed_password.
3. Drops user_tokens (password-reset and email-confirmation links).

The superadmin is restored by signing in with SUPERADMIN_EMAIL, or with
scripts/create_superadmin.py. Downgrade restores the column and table, empty;
deleted accounts cannot be brought back.

Revision ID: b8d1e4f2a6c9
Revises: a3e6c9d2f7b1
"""

import sqlalchemy as sa
from alembic import op

revision = "b8d1e4f2a6c9"
down_revision = "a3e6c9d2f7b1"
branch_labels = None
depends_on = None


def _delete_password_accounts(bind) -> None:
    users = sa.table("users", sa.column("id"))
    identities = sa.table("external_identities", sa.column("user_id"))
    doomed = [
        row[0]
        for row in bind.execute(
            sa.select(users.c.id).where(users.c.id.not_in(sa.select(identities.c.user_id)))
        )
    ]
    if not doomed:
        return

    inspector = sa.inspect(bind)
    for table_name in inspector.get_table_names():
        for fk in inspector.get_foreign_keys(table_name):
            if fk.get("referred_table") != "users":
                continue
            column = fk["constrained_columns"][0]
            table = sa.table(table_name, sa.column(column))
            match = table.c[column].in_(doomed)
            if (fk.get("options") or {}).get("ondelete", "").upper() == "CASCADE":
                bind.execute(table.delete().where(match))
            else:
                bind.execute(table.update().where(match).values({column: None}))
    bind.execute(users.delete().where(users.c.id.in_(doomed)))


def upgrade() -> None:
    _delete_password_accounts(op.get_bind())

    with op.batch_alter_table("users") as batch:
        batch.drop_column("hashed_password")

    op.drop_index("ix_user_tokens_user_id", table_name="user_tokens")
    op.drop_table("user_tokens")


def downgrade() -> None:
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
        batch.add_column(sa.Column("hashed_password", sa.String(length=255), nullable=True))
