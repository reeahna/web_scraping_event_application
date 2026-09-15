"""review_status defaults to reviewed

Every event was created with review_status="needs_review", so the flag never
distinguished anything: the whole table sat in one bucket and the admin filter
offered "all events" or "no events". The status now means "something flagged
this" — set by app.services.geographic_filter.geo_needs_review (missing or
ambiguous geography) or by an administrator.

Existing rows carry no information to preserve: every one of them is at the old
default, none was ever reviewed or deliberately flagged, so they all move to
"reviewed". Anything genuinely geo-ambiguous is re-flagged on its next
extraction run — the geo check in app.services.extraction_runs runs on updated
events, not only newly inserted ones.

The downgrade deliberately does NOT restore "needs_review" for every row: that
would invent a flag on events that were never flagged. It only restores the
column default, so new events after a downgrade behave as they used to.

Revision ID: c4a7e2b91d63
Revises: b2d9f1a7c4e5
"""

from alembic import op

revision = "c4a7e2b91d63"
down_revision = "b2d9f1a7c4e5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE events SET review_status = 'reviewed' WHERE review_status = 'needs_review'"
    )


def downgrade() -> None:
    # Intentionally a no-op on data: see the module docstring. The model default
    # is what changes back, and that lives in code, not in the schema (the
    # column has no server_default).
    pass
