from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.auto_onboarding import ACTOR_SYSTEM, SYSTEM_ACTOR_LABEL
from app.database import Base
from app.models.base import utcnow

if TYPE_CHECKING:
    from app.models.audit_log import AuditLog
    from app.models.auto_onboarding_decision import AutoOnboardingDecision
    from app.models.website import Website


class AutoOnboardingActionResult(Base):
    """What was actually attempted because of a decision, and how it went.

    Kept out of AutoOnboardingDecision so that decision rows stay immutable
    evaluation snapshots. One decision can produce two results — an approval
    and, only if that approval succeeded, a separate activation — and a
    partial success (approved but not activated) is therefore visible as
    exactly that: one succeeded row and one failed row, never a single
    ambiguous record.
    """

    __tablename__ = "auto_onboarding_action_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    decision_id: Mapped[int] = mapped_column(
        ForeignKey("auto_onboarding_decisions.id", ondelete="CASCADE"), index=True
    )
    website_id: Mapped[int] = mapped_column(
        ForeignKey("websites.id", ondelete="CASCADE"), index=True
    )
    action_type: Mapped[str] = mapped_column(String(16), index=True)

    attempted: Mapped[bool] = mapped_column(Boolean, default=True)
    succeeded: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(String(500), default=None)

    actor_type: Mapped[str] = mapped_column(String(16), default=ACTOR_SYSTEM)
    actor_label: Mapped[str | None] = mapped_column(String(64), default=SYSTEM_ACTOR_LABEL)
    # The audit entry this action wrote, when one was created — so the audit
    # log and the decision history can be read from either end.
    audit_log_id: Mapped[int | None] = mapped_column(
        ForeignKey("audit_logs.id", ondelete="SET NULL"), default=None
    )
    # The configuration version the action operated on, recorded so a later
    # draft edit is visibly not what was approved or activated.
    configuration_version: Mapped[int | None] = mapped_column(Integer, default=None)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    decision: Mapped["AutoOnboardingDecision"] = relationship(back_populates="action_results")
    website: Mapped["Website"] = relationship()
    audit_log: Mapped["AuditLog | None"] = relationship()
