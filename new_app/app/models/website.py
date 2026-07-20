from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.onboarding import DRAFT
from app.database import Base
from app.models.base import TimestampMixin

if TYPE_CHECKING:
    from app.models.city import City
    from app.models.event import Event


class Website(Base, TimestampMixin):
    """A specific source site scraped for a city (e.g. an Eventbrite city page)."""

    __tablename__ = "websites"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    # Human-readable attribution shown on scraped events, distinct from the
    # internal admin-facing `name`.
    source_display_name: Mapped[str | None] = mapped_column(String(255), default=None)
    city_id: Mapped[int | None] = mapped_column(
        ForeignKey("cities.id", ondelete="SET NULL"), default=None
    )
    base_url: Mapped[str] = mapped_column(String(2000))
    event_listing_url: Mapped[str | None] = mapped_column(String(2000), default=None)
    # Overrides the city's timezone for this source's events, when set.
    timezone_override: Mapped[str | None] = mapped_column(String(64), default=None)
    requires_js: Mapped[bool] = mapped_column(Boolean, default=False)

    # Onboarding lifecycle (see app.core.onboarding) is the source of truth;
    # is_active is kept in sync with it (True only when onboarding_status ==
    # "active") purely so existing sites.activate-style queries/filters stay
    # simple. Both are only ever changed together, via the transition service.
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    onboarding_status: Mapped[str] = mapped_column(String(32), default=DRAFT)

    # `proposed_pattern` = the detector's suggestion, awaiting review.
    # `approved_pattern` = a FROZEN, self-contained snapshot of the full
    # SiteConfiguration (not just a pattern-name pointer) as it was at
    # approval time — persistent extraction reads only this, never the live
    # `configuration` draft, so editing the draft after approval has zero
    # effect on live extraction until an explicit re-approve.
    proposed_pattern: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    approved_pattern: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    configuration: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    schedule_config: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)

    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    approved_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    # Bumped every time `configuration` (the editable draft) is saved.
    configuration_version: Mapped[int] = mapped_column(Integer, default=0)
    # The configuration_version that was live at the moment it was copied
    # into `approved_pattern` — None until the first approval.
    active_configuration_version: Mapped[int | None] = mapped_column(Integer, default=None)

    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    consecutive_failure_count: Mapped[int] = mapped_column(Integer, default=0)

    # Prerequisite (along with events) for safely deleting the website's city.
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    city: Mapped["City | None"] = relationship(back_populates="websites")
    events: Mapped[list["Event"]] = relationship(back_populates="website")
