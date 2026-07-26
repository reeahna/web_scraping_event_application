"""phase 8d automatic onboarding policy

Revision ID: d7a1c4e83b60
Revises: b2f4a7c91d05

Adds the policy, assignment, decision and action-result tables behind
policy-controlled automatic approval and activation, plus two audit columns
that let a system action be told apart from a human one, and a column
recording how a Website's draft configuration was produced.

Self-contained by design: it imports nothing from the application (no
PatternRegistry, no services, no seeders), because a historical migration
must keep working after those move or change. The seeded default policy is
therefore written with plain table/insert constructs and conservative literal
values — notably an empty allowed-pattern list, which is meaningful without
needing to know which patterns exist. Runtime validation of pattern names
against PatternRegistry happens in the application, not here.

Non-destructive: every added column is nullable or server-defaulted, and all
four tables are new with no dependency on existing rows. Installing this
revision cannot change any existing Website's onboarding outcome, because the
seeded policy has automatic approval and activation switched off.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d7a1c4e83b60"
down_revision: str | None = "b2f4a7c91d05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DEFAULT_POLICY_NAME = "Conservative default"
_DEFAULT_POLICY_DESCRIPTION = (
    "Conservative default: automatic configuration and preview are enabled, "
    "automatic approval and activation are not. Installing automatic onboarding "
    "must not change any existing source's outcome."
)


def _float_column(name: str, default: str) -> sa.Column:
    return sa.Column(name, sa.Float(), nullable=False, server_default=sa.text(default))


def _int_column(name: str, default: str) -> sa.Column:
    return sa.Column(name, sa.Integer(), nullable=False, server_default=sa.text(default))


def _bool_column(name: str, default: str) -> sa.Column:
    return sa.Column(name, sa.Boolean(), nullable=False, server_default=sa.text(default))


def upgrade() -> None:
    op.create_table(
        "auto_onboarding_policies",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        _bool_column("active", "1"),
        _int_column("version", "1"),
        _bool_column("is_global_default", "0"),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column("updated_by_user_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        # Workflow enablement
        _bool_column("automatic_configuration_enabled", "1"),
        _bool_column("automatic_preview_enabled", "1"),
        _bool_column("automatic_approval_enabled", "0"),
        _bool_column("automatic_activation_enabled", "0"),
        # Pattern and configuration-origin controls
        sa.Column("allowed_pattern_names", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        _bool_column("allow_generic_html_cards", "0"),
        _bool_column("allow_browser_required", "0"),
        _bool_column("allow_ai_origin", "0"),
        _bool_column("allow_administrator_manual_origin", "0"),
        _bool_column("allow_imported_configuration", "0"),
        _bool_column("allow_detail_page_enrichment", "1"),
        # General quality thresholds
        _float_column("minimum_detector_confidence", "0.8"),
        _int_column("minimum_events_found", "3"),
        _int_column("minimum_valid_events", "3"),
        _float_column("minimum_valid_percentage", "0.9"),
        _float_column("maximum_rejected_percentage", "0.1"),
        _float_column("minimum_canonical_url_coverage", "1.0"),
        _float_column("minimum_start_date_coverage", "1.0"),
        _float_column("minimum_start_date_parse_success", "0.95"),
        _float_column("maximum_duplicate_rate", "0.2"),
        _int_column("maximum_warning_count", "10"),
        _int_column("maximum_critical_warning_count", "0"),
        _int_column("minimum_distinct_events", "3"),
        _bool_column("require_absolute_public_urls", "1"),
        _bool_column("require_zero_critical_warnings", "1"),
        _bool_column("require_distinct_events", "1"),
        # Stricter generic_html_cards thresholds
        _float_column("generic_html_minimum_detector_confidence", "0.85"),
        _int_column("generic_html_minimum_events_found", "5"),
        _int_column("generic_html_minimum_valid_events", "5"),
        _float_column("generic_html_minimum_valid_percentage", "0.95"),
        _float_column("generic_html_maximum_rejected_percentage", "0.05"),
        _float_column("generic_html_minimum_required_field_confidence", "0.75"),
        _float_column("generic_html_minimum_required_field_coverage", "1.0"),
        _float_column("generic_html_minimum_date_format_confidence", "0.9"),
        _int_column("generic_html_minimum_distinct_event_count", "5"),
        _bool_column("generic_html_reject_broad_canonical_selector", "1"),
        _bool_column("generic_html_reject_unstable_required_selectors", "1"),
        sa.UniqueConstraint("name", name="uq_auto_onboarding_policies_name"),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name="fk_auto_onboarding_policies_created_by",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_user_id"],
            ["users.id"],
            name="fk_auto_onboarding_policies_updated_by",
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_auto_onboarding_policies_active", "auto_onboarding_policies", ["active"]
    )
    # At most one ACTIVE global default can exist. A partial unique index makes
    # that a database guarantee rather than a convention the service has to
    # remember; deactivated former defaults are unaffected.
    op.create_index(
        "uq_auto_onboarding_policies_active_global_default",
        "auto_onboarding_policies",
        ["is_global_default"],
        unique=True,
        sqlite_where=sa.text("is_global_default = 1 AND active = 1"),
        postgresql_where=sa.text("is_global_default AND active"),
    )

    op.create_table(
        "auto_onboarding_policy_cities",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("policy_id", sa.Integer(), nullable=False),
        sa.Column("city_id", sa.Integer(), nullable=False),
        # Unique across the table, not per policy: one city, at most one
        # policy, so city precedence can never be ambiguous.
        sa.UniqueConstraint("city_id", name="uq_auto_onboarding_policy_city"),
        sa.ForeignKeyConstraint(
            ["policy_id"],
            ["auto_onboarding_policies.id"],
            name="fk_auto_onboarding_policy_cities_policy",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["city_id"],
            ["cities.id"],
            name="fk_auto_onboarding_policy_cities_city",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_auto_onboarding_policy_cities_policy_id",
        "auto_onboarding_policy_cities",
        ["policy_id"],
    )
    op.create_index(
        "ix_auto_onboarding_policy_cities_city_id", "auto_onboarding_policy_cities", ["city_id"]
    )

    op.create_table(
        "auto_onboarding_policy_roles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("policy_id", sa.Integer(), nullable=False),
        sa.Column("role_id", sa.Integer(), nullable=False),
        sa.UniqueConstraint("policy_id", "role_id", name="uq_auto_onboarding_policy_role"),
        sa.ForeignKeyConstraint(
            ["policy_id"],
            ["auto_onboarding_policies.id"],
            name="fk_auto_onboarding_policy_roles_policy",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["role_id"],
            ["roles.id"],
            name="fk_auto_onboarding_policy_roles_role",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_auto_onboarding_policy_roles_policy_id", "auto_onboarding_policy_roles", ["policy_id"]
    )
    op.create_index(
        "ix_auto_onboarding_policy_roles_role_id", "auto_onboarding_policy_roles", ["role_id"]
    )

    op.create_table(
        "auto_onboarding_decisions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("website_id", sa.Integer(), nullable=False),
        sa.Column("onboarding_job_id", sa.Integer(), nullable=True),
        sa.Column("onboarding_batch_id", sa.Integer(), nullable=True),
        sa.Column("policy_id", sa.Integer(), nullable=True),
        sa.Column("policy_version", sa.Integer(), nullable=True),
        sa.Column(
            "decision_kind", sa.String(length=32), nullable=False, server_default="onboarding"
        ),
        sa.Column("final_decision", sa.String(length=48), nullable=False),
        _bool_column("eligible_for_automatic_approval", "0"),
        _bool_column("eligible_for_automatic_activation", "0"),
        _bool_column("activation_policy_enabled", "0"),
        sa.Column("detected_pattern", sa.String(length=64), nullable=True),
        sa.Column("detector_confidence", sa.Float(), nullable=True),
        sa.Column("configuration_origin", sa.String(length=48), nullable=True),
        sa.Column("configuration_version", sa.Integer(), nullable=True),
        sa.Column("preview_run_id", sa.Integer(), nullable=True),
        sa.Column("preview_status", sa.String(length=16), nullable=True),
        sa.Column("metrics_snapshot", sa.JSON(), nullable=True),
        sa.Column("thresholds_snapshot", sa.JSON(), nullable=True),
        sa.Column("reasons_passed", sa.JSON(), nullable=True),
        sa.Column("reasons_failed", sa.JSON(), nullable=True),
        sa.Column(
            "system_actor_type", sa.String(length=16), nullable=False, server_default="system"
        ),
        sa.Column("submitted_by_user_id", sa.Integer(), nullable=True),
        sa.Column("evaluated_roles", sa.JSON(), nullable=True),
        sa.Column("reevaluates_decision_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["website_id"],
            ["websites.id"],
            name="fk_auto_onboarding_decisions_website",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["onboarding_job_id"],
            ["onboarding_jobs.id"],
            name="fk_auto_onboarding_decisions_job",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["onboarding_batch_id"],
            ["onboarding_batches.id"],
            name="fk_auto_onboarding_decisions_batch",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["policy_id"],
            ["auto_onboarding_policies.id"],
            name="fk_auto_onboarding_decisions_policy",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["preview_run_id"],
            ["extraction_runs.id"],
            name="fk_auto_onboarding_decisions_preview_run",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["submitted_by_user_id"],
            ["users.id"],
            name="fk_auto_onboarding_decisions_submitted_by",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["reevaluates_decision_id"],
            ["auto_onboarding_decisions.id"],
            name="fk_auto_onboarding_decisions_reevaluates",
            ondelete="SET NULL",
        ),
    )
    for column in ("website_id", "onboarding_job_id", "onboarding_batch_id", "policy_id"):
        op.create_index(
            f"ix_auto_onboarding_decisions_{column}", "auto_onboarding_decisions", [column]
        )
    op.create_index(
        "ix_auto_onboarding_decisions_final_decision",
        "auto_onboarding_decisions",
        ["final_decision"],
    )

    op.create_table(
        "auto_onboarding_action_results",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("decision_id", sa.Integer(), nullable=False),
        sa.Column("website_id", sa.Integer(), nullable=False),
        sa.Column("action_type", sa.String(length=16), nullable=False),
        _bool_column("attempted", "1"),
        _bool_column("succeeded", "0"),
        sa.Column("error", sa.String(length=500), nullable=True),
        sa.Column("actor_type", sa.String(length=16), nullable=False, server_default="system"),
        sa.Column("actor_label", sa.String(length=64), nullable=True),
        sa.Column("audit_log_id", sa.Integer(), nullable=True),
        sa.Column("configuration_version", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["decision_id"],
            ["auto_onboarding_decisions.id"],
            name="fk_auto_onboarding_action_results_decision",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["website_id"],
            ["websites.id"],
            name="fk_auto_onboarding_action_results_website",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["audit_log_id"],
            ["audit_logs.id"],
            name="fk_auto_onboarding_action_results_audit_log",
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_auto_onboarding_action_results_decision_id",
        "auto_onboarding_action_results",
        ["decision_id"],
    )
    op.create_index(
        "ix_auto_onboarding_action_results_website_id",
        "auto_onboarding_action_results",
        ["website_id"],
    )
    op.create_index(
        "ix_auto_onboarding_action_results_action_type",
        "auto_onboarding_action_results",
        ["action_type"],
    )

    # Audit actor support. Existing rows keep their meaning: every historical
    # entry was written by a person, so "user" is the correct backfill.
    with op.batch_alter_table("audit_logs") as batch_op:
        batch_op.add_column(
            sa.Column(
                "actor_type", sa.String(length=16), nullable=False, server_default="user"
            )
        )
        batch_op.add_column(sa.Column("actor_label", sa.String(length=64), nullable=True))

    # How a Website's draft configuration was produced. NULL for every
    # pre-existing row, which the policy treats as unknown and therefore not
    # automatically approvable.
    with op.batch_alter_table("websites") as batch_op:
        batch_op.add_column(sa.Column("configuration_origin", sa.String(length=48), nullable=True))

    # Optional batch-level policy override (Phase 8D completion). Nullable, so
    # every existing batch keeps resolving via city/global precedence.
    with op.batch_alter_table("onboarding_batches") as batch_op:
        batch_op.add_column(sa.Column("selected_policy_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_onboarding_batches_selected_policy",
            "auto_onboarding_policies",
            ["selected_policy_id"],
            ["id"],
            ondelete="SET NULL",
        )

    _seed_default_policy()


def _seed_default_policy() -> None:
    """Idempotent insert of the conservative default policy.

    Values are literals rather than imports so this revision keeps behaving
    the same after the application's defaults change. Every automatic action
    is off.
    """
    policies = sa.table(
        "auto_onboarding_policies",
        sa.column("name", sa.String),
        sa.column("description", sa.Text),
        sa.column("active", sa.Boolean),
        sa.column("version", sa.Integer),
        sa.column("is_global_default", sa.Boolean),
        sa.column("automatic_configuration_enabled", sa.Boolean),
        sa.column("automatic_preview_enabled", sa.Boolean),
        sa.column("automatic_approval_enabled", sa.Boolean),
        sa.column("automatic_activation_enabled", sa.Boolean),
        sa.column("allowed_pattern_names", sa.JSON),
        sa.column("allow_generic_html_cards", sa.Boolean),
        sa.column("allow_browser_required", sa.Boolean),
        sa.column("allow_ai_origin", sa.Boolean),
        sa.column("allow_administrator_manual_origin", sa.Boolean),
        sa.column("allow_imported_configuration", sa.Boolean),
        sa.column("allow_detail_page_enrichment", sa.Boolean),
    )
    connection = op.get_bind()
    existing = connection.execute(
        sa.select(sa.literal_column("1"))
        .select_from(sa.table("auto_onboarding_policies", sa.column("name", sa.String)))
        .where(sa.column("name") == _DEFAULT_POLICY_NAME)
    ).first()
    if existing is not None:
        return

    connection.execute(
        policies.insert().values(
            name=_DEFAULT_POLICY_NAME,
            description=_DEFAULT_POLICY_DESCRIPTION,
            active=True,
            version=1,
            is_global_default=True,
            automatic_configuration_enabled=True,
            automatic_preview_enabled=True,
            automatic_approval_enabled=False,
            automatic_activation_enabled=False,
            allowed_pattern_names=[],
            allow_generic_html_cards=False,
            allow_browser_required=False,
            allow_ai_origin=False,
            allow_administrator_manual_origin=False,
            allow_imported_configuration=False,
            allow_detail_page_enrichment=True,
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("onboarding_batches") as batch_op:
        batch_op.drop_constraint("fk_onboarding_batches_selected_policy", type_="foreignkey")
        batch_op.drop_column("selected_policy_id")
    with op.batch_alter_table("websites") as batch_op:
        batch_op.drop_column("configuration_origin")
    with op.batch_alter_table("audit_logs") as batch_op:
        batch_op.drop_column("actor_label")
        batch_op.drop_column("actor_type")

    op.drop_index(
        "ix_auto_onboarding_action_results_action_type",
        table_name="auto_onboarding_action_results",
    )
    op.drop_index(
        "ix_auto_onboarding_action_results_website_id",
        table_name="auto_onboarding_action_results",
    )
    op.drop_index(
        "ix_auto_onboarding_action_results_decision_id",
        table_name="auto_onboarding_action_results",
    )
    op.drop_table("auto_onboarding_action_results")

    op.drop_index(
        "ix_auto_onboarding_decisions_final_decision", table_name="auto_onboarding_decisions"
    )
    for column in ("policy_id", "onboarding_batch_id", "onboarding_job_id", "website_id"):
        op.drop_index(
            f"ix_auto_onboarding_decisions_{column}", table_name="auto_onboarding_decisions"
        )
    op.drop_table("auto_onboarding_decisions")

    op.drop_index(
        "ix_auto_onboarding_policy_roles_role_id", table_name="auto_onboarding_policy_roles"
    )
    op.drop_index(
        "ix_auto_onboarding_policy_roles_policy_id", table_name="auto_onboarding_policy_roles"
    )
    op.drop_table("auto_onboarding_policy_roles")

    op.drop_index(
        "ix_auto_onboarding_policy_cities_city_id", table_name="auto_onboarding_policy_cities"
    )
    op.drop_index(
        "ix_auto_onboarding_policy_cities_policy_id", table_name="auto_onboarding_policy_cities"
    )
    op.drop_table("auto_onboarding_policy_cities")

    op.drop_index(
        "uq_auto_onboarding_policies_active_global_default",
        table_name="auto_onboarding_policies",
    )
    op.drop_index("ix_auto_onboarding_policies_active", table_name="auto_onboarding_policies")
    op.drop_table("auto_onboarding_policies")
