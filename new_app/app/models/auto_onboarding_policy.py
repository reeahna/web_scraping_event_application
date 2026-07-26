from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.base import TimestampMixin

if TYPE_CHECKING:
    from app.models.city import City
    from app.models.role import Role
    from app.models.user import User


class AutoOnboardingPolicy(Base, TimestampMixin):
    """Administrator-authored rules for when a source may be approved, and
    separately activated, without a human.

    Every threshold is a normalized column so it can be queried, validated and
    edited through a structured form. The single JSON column
    (`allowed_pattern_names`) holds a bounded list validated at runtime
    against PatternRegistry — never an expression, never code.

    Conservative by construction: the seeded default has automatic approval
    and activation off, generic_html_cards off, and browser-required and
    AI-origin configurations denied, so installing this phase cannot change
    any existing source's outcome.
    """

    __tablename__ = "auto_onboarding_policies"
    __table_args__ = (
        # At most one ACTIVE global default, enforced by the database rather
        # than by a service remembering to check. Declared here as well as in
        # the migration so a schema built with create_all (the test suite) has
        # exactly the same guarantee as a migrated one.
        Index(
            "uq_auto_onboarding_policies_active_global_default",
            "is_global_default",
            unique=True,
            sqlite_where=text("is_global_default = 1 AND active = 1"),
            postgresql_where=text("is_global_default AND active"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    description: Mapped[str | None] = mapped_column(Text, default=None)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    # Bumped by the service on any meaningful change; decisions record the
    # version they were evaluated under and are never rewritten afterwards.
    version: Mapped[int] = mapped_column(Integer, default=1)
    is_global_default: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    updated_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )

    # --- Workflow enablement ----------------------------------------------
    automatic_configuration_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    automatic_preview_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    automatic_approval_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    automatic_activation_enabled: Mapped[bool] = mapped_column(Boolean, default=False)

    # --- Pattern and configuration-origin controls ------------------------
    # Bounded list of registered pattern names; empty means "no pattern is
    # allowed to be approved automatically".
    allowed_pattern_names: Mapped[list] = mapped_column(JSON, default=list)
    allow_generic_html_cards: Mapped[bool] = mapped_column(Boolean, default=False)
    allow_browser_required: Mapped[bool] = mapped_column(Boolean, default=False)
    allow_ai_origin: Mapped[bool] = mapped_column(Boolean, default=False)
    allow_administrator_manual_origin: Mapped[bool] = mapped_column(Boolean, default=False)
    allow_imported_configuration: Mapped[bool] = mapped_column(Boolean, default=False)
    allow_detail_page_enrichment: Mapped[bool] = mapped_column(Boolean, default=True)

    # --- General quality thresholds ---------------------------------------
    minimum_detector_confidence: Mapped[float] = mapped_column(Float, default=0.80)
    minimum_events_found: Mapped[int] = mapped_column(Integer, default=3)
    minimum_valid_events: Mapped[int] = mapped_column(Integer, default=3)
    minimum_valid_percentage: Mapped[float] = mapped_column(Float, default=0.90)
    maximum_rejected_percentage: Mapped[float] = mapped_column(Float, default=0.10)
    minimum_canonical_url_coverage: Mapped[float] = mapped_column(Float, default=1.0)
    minimum_start_date_coverage: Mapped[float] = mapped_column(Float, default=1.0)
    minimum_start_date_parse_success: Mapped[float] = mapped_column(Float, default=0.95)
    maximum_duplicate_rate: Mapped[float] = mapped_column(Float, default=0.20)
    maximum_warning_count: Mapped[int] = mapped_column(Integer, default=10)
    maximum_critical_warning_count: Mapped[int] = mapped_column(Integer, default=0)
    minimum_distinct_events: Mapped[int] = mapped_column(Integer, default=3)
    require_absolute_public_urls: Mapped[bool] = mapped_column(Boolean, default=True)
    require_zero_critical_warnings: Mapped[bool] = mapped_column(Boolean, default=True)
    require_distinct_events: Mapped[bool] = mapped_column(Boolean, default=True)

    # --- Stricter thresholds for generic_html_cards -----------------------
    # An inferred-from-markup configuration has no schema behind it, so it
    # carries its own, higher bar rather than sharing the structured one.
    generic_html_minimum_detector_confidence: Mapped[float] = mapped_column(Float, default=0.85)
    generic_html_minimum_events_found: Mapped[int] = mapped_column(Integer, default=5)
    generic_html_minimum_valid_events: Mapped[int] = mapped_column(Integer, default=5)
    generic_html_minimum_valid_percentage: Mapped[float] = mapped_column(Float, default=0.95)
    generic_html_maximum_rejected_percentage: Mapped[float] = mapped_column(Float, default=0.05)
    generic_html_minimum_required_field_confidence: Mapped[float] = mapped_column(
        Float, default=0.75
    )
    generic_html_minimum_required_field_coverage: Mapped[float] = mapped_column(Float, default=1.0)
    generic_html_minimum_date_format_confidence: Mapped[float] = mapped_column(Float, default=0.90)
    generic_html_minimum_distinct_event_count: Mapped[int] = mapped_column(Integer, default=5)
    generic_html_reject_broad_canonical_selector: Mapped[bool] = mapped_column(
        Boolean, default=True
    )
    generic_html_reject_unstable_required_selectors: Mapped[bool] = mapped_column(
        Boolean, default=True
    )

    created_by: Mapped["User | None"] = relationship(foreign_keys=[created_by_user_id])
    updated_by: Mapped["User | None"] = relationship(foreign_keys=[updated_by_user_id])
    city_assignments: Mapped[list["AutoOnboardingPolicyCity"]] = relationship(
        back_populates="policy", cascade="all, delete-orphan"
    )
    role_assignments: Mapped[list["AutoOnboardingPolicyRole"]] = relationship(
        back_populates="policy", cascade="all, delete-orphan"
    )


class AutoOnboardingPolicyCity(Base):
    """City -> policy assignment.

    `city_id` is unique across the whole table, not just per policy: a city
    can have at most one assigned policy, which is what makes the
    city-precedence step unambiguous by construction rather than by
    convention.
    """

    __tablename__ = "auto_onboarding_policy_cities"
    __table_args__ = (UniqueConstraint("city_id", name="uq_auto_onboarding_policy_city"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    policy_id: Mapped[int] = mapped_column(
        ForeignKey("auto_onboarding_policies.id", ondelete="CASCADE"), index=True
    )
    city_id: Mapped[int] = mapped_column(ForeignKey("cities.id", ondelete="CASCADE"), index=True)

    policy: Mapped["AutoOnboardingPolicy"] = relationship(back_populates="city_assignments")
    city: Mapped["City"] = relationship()


class AutoOnboardingPolicyRole(Base):
    """Restricts a policy to submissions made by users holding one of these
    roles. Empty means "any submitter"."""

    __tablename__ = "auto_onboarding_policy_roles"
    __table_args__ = (
        UniqueConstraint("policy_id", "role_id", name="uq_auto_onboarding_policy_role"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    policy_id: Mapped[int] = mapped_column(
        ForeignKey("auto_onboarding_policies.id", ondelete="CASCADE"), index=True
    )
    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id", ondelete="CASCADE"), index=True)

    policy: Mapped["AutoOnboardingPolicy"] = relationship(back_populates="role_assignments")
    role: Mapped["Role"] = relationship()
