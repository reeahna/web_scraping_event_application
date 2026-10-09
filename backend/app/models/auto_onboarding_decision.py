from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.auto_onboarding import ACTOR_SYSTEM, DECISION_ONBOARDING
from app.database import Base
from app.models.base import utcnow

if TYPE_CHECKING:
    from app.models.auto_onboarding_action_result import AutoOnboardingActionResult
    from app.models.auto_onboarding_policy import AutoOnboardingPolicy
    from app.models.extraction_run import ExtractionRun
    from app.models.onboarding_batch import OnboardingBatch
    from app.models.onboarding_job import OnboardingJob
    from app.models.user import User
    from app.models.website import Website


class AutoOnboardingDecision(Base):
    """One immutable evaluation of a Website against a policy.

    **Append-only in the strict sense**: every column here describes the
    evaluation, and nothing writes to a row after it is created. What
    *happened* as a result — an approval attempt, an activation attempt,
    whether either succeeded — lives in AutoOnboardingActionResult rows that
    point back here. That split is the whole reason the two models exist: a
    decision that recorded "eligible" and was later mutated to "approved"
    would no longer be a record of what was decided.

    Re-evaluating a Website never edits the old row; it inserts a new one
    with `reevaluates_decision_id` pointing at its predecessor.
    """

    __tablename__ = "auto_onboarding_decisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    website_id: Mapped[int] = mapped_column(
        ForeignKey("websites.id", ondelete="CASCADE"), index=True
    )
    onboarding_job_id: Mapped[int | None] = mapped_column(
        ForeignKey("onboarding_jobs.id", ondelete="SET NULL"), index=True, default=None
    )
    onboarding_batch_id: Mapped[int | None] = mapped_column(
        ForeignKey("onboarding_batches.id", ondelete="SET NULL"), index=True, default=None
    )

    # Nullable: "no applicable policy" is itself a decision worth recording.
    policy_id: Mapped[int | None] = mapped_column(
        ForeignKey("auto_onboarding_policies.id", ondelete="SET NULL"), index=True, default=None
    )
    # Copied, not joined — the policy may be edited later, and this decision
    # must keep saying which version it was evaluated under.
    policy_version: Mapped[int | None] = mapped_column(Integer, default=None)

    decision_kind: Mapped[str] = mapped_column(String(32), default=DECISION_ONBOARDING)
    final_decision: Mapped[str] = mapped_column(String(48), index=True)

    eligible_for_automatic_approval: Mapped[bool] = mapped_column(Boolean, default=False)
    # "The policy permits activation and nothing in this evaluation forbids
    # it" — deliberately NOT a promise that activation will happen. Final
    # activation eligibility is re-evaluated after approval actually succeeds
    # (see the activation re-check in the execution service).
    eligible_for_automatic_activation: Mapped[bool] = mapped_column(Boolean, default=False)
    activation_policy_enabled: Mapped[bool] = mapped_column(Boolean, default=False)

    detected_pattern: Mapped[str | None] = mapped_column(String(64), default=None)
    detector_confidence: Mapped[float | None] = mapped_column(Float, default=None)
    configuration_origin: Mapped[str | None] = mapped_column(String(48), default=None)
    configuration_version: Mapped[int | None] = mapped_column(Integer, default=None)
    preview_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("extraction_runs.id", ondelete="SET NULL"), default=None
    )
    preview_status: Mapped[str | None] = mapped_column(String(16), default=None)

    # Compact and reproducible: only the metrics and thresholds this
    # evaluation actually consulted, never a copy of every policy column.
    metrics_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    thresholds_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    reasons_passed: Mapped[list] = mapped_column(JSON, default=list)
    reasons_failed: Mapped[list] = mapped_column(JSON, default=list)

    system_actor_type: Mapped[str] = mapped_column(String(16), default=ACTOR_SYSTEM)
    # The human who submitted the source, kept distinct from the actor: they
    # did not approve anything, and their permissions did not authorize it.
    submitted_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    # Role codes evaluated at submission time, so a later role change cannot
    # rewrite what this decision was based on.
    evaluated_roles: Mapped[list | None] = mapped_column(JSON, default=None)

    reevaluates_decision_id: Mapped[int | None] = mapped_column(
        ForeignKey("auto_onboarding_decisions.id", ondelete="SET NULL"), default=None
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    website: Mapped["Website"] = relationship()
    policy: Mapped["AutoOnboardingPolicy | None"] = relationship()
    onboarding_job: Mapped["OnboardingJob | None"] = relationship()
    onboarding_batch: Mapped["OnboardingBatch | None"] = relationship()
    preview_run: Mapped["ExtractionRun | None"] = relationship()
    submitted_by: Mapped["User | None"] = relationship()
    reevaluates: Mapped["AutoOnboardingDecision | None"] = relationship(remote_side=[id])
    action_results: Mapped[list["AutoOnboardingActionResult"]] = relationship(
        back_populates="decision",
        cascade="all, delete-orphan",
        order_by="AutoOnboardingActionResult.id",
    )
