"""A request to onboard a whole college town with Claude.

An administrator types a town (and optionally its state and schools); the
scheduler process asks Claude to find the town's event listing pages, on
campus and in town, then creates the city (when it does not exist yet) and
queues every page as an ordinary onboarding batch. The row keeps what Claude
proposed, so the result can be reviewed and downloaded as a source CSV.
"""

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.base import utcnow

if TYPE_CHECKING:
    from app.models.city import City
    from app.models.onboarding_batch import OnboardingBatch
    from app.models.user import User

TOWN_QUEUED = "queued"
TOWN_RUNNING = "running"
TOWN_DONE = "done"
TOWN_FAILED = "failed"
TOWN_STATUSES: tuple[str, ...] = (TOWN_QUEUED, TOWN_RUNNING, TOWN_DONE, TOWN_FAILED)


class TownOnboardingRequest(Base):
    __tablename__ = "town_onboarding_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    town_name: Mapped[str] = mapped_column(String(255))
    state_or_region: Mapped[str | None] = mapped_column(String(100), default=None)
    # School names the administrator supplied, as a list of strings.
    universities: Mapped[list[str] | None] = mapped_column(JSON, default=None)

    status: Mapped[str] = mapped_column(String(16), default=TOWN_QUEUED, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    failure_reason: Mapped[str | None] = mapped_column(String(500), default=None)

    # What Claude proposed (TownProposal.as_dict()): the city record, the
    # sources and the token usage. Kept even when applying it failed.
    proposal: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)

    city_id: Mapped[int | None] = mapped_column(
        ForeignKey("cities.id", ondelete="SET NULL"), default=None
    )
    batch_id: Mapped[int | None] = mapped_column(
        ForeignKey("onboarding_batches.id", ondelete="SET NULL"), default=None
    )
    requested_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    city: Mapped["City | None"] = relationship()
    batch: Mapped["OnboardingBatch | None"] = relationship()
    requested_by: Mapped["User | None"] = relationship()
