from datetime import date, datetime, time
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, String, Text, Time
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.base import TimestampMixin

if TYPE_CHECKING:
    from app.models.categorization_rule import CategorizationRule
    from app.models.city import City
    from app.models.event_category import EventCategory
    from app.models.website import Website


class Event(Base, TimestampMixin):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)

    title: Mapped[str] = mapped_column(String(500))
    normalized_title: Mapped[str | None] = mapped_column(String(500), index=True, default=None)
    canonical_url: Mapped[str] = mapped_column(String(2000), index=True)
    source: Mapped[str] = mapped_column(String(255))
    # Source-provided ID, when available — more reliable than the URL for identifying
    # the same event across re-scrapes of a site that reuses/rewrites URLs.
    external_source_id: Mapped[str | None] = mapped_column(String(255), index=True, default=None)
    # Reserved for a future content-based dedup hash (title+venue+start_date, etc.);
    # not populated or enforced unique yet — deliberately left for a later phase.
    fingerprint: Mapped[str | None] = mapped_column(String(64), index=True, default=None)

    description: Mapped[str | None] = mapped_column(Text, default=None)
    source_category: Mapped[str | None] = mapped_column(String(255), default=None)
    timezone: Mapped[str | None] = mapped_column(String(64), default=None)
    origin: Mapped[str] = mapped_column(String(32), default="extracted")

    start_date: Mapped[date | None] = mapped_column(Date, default=None)
    end_date: Mapped[date | None] = mapped_column(Date, default=None)
    start_time: Mapped[time | None] = mapped_column(Time, default=None)
    end_time: Mapped[time | None] = mapped_column(Time, default=None)

    venue: Mapped[str | None] = mapped_column(String(500), default=None)
    address: Mapped[str | None] = mapped_column(String(1000), default=None)
    image_url: Mapped[str | None] = mapped_column(String(2000), default=None)

    latitude: Mapped[float | None] = mapped_column(Float, default=None)
    longitude: Mapped[float | None] = mapped_column(Float, default=None)

    scraped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Distinct from is_active: archiving is a deliberate curatorial step (a
    # prerequisite for deleting the event's city), not just a visibility toggle.
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    review_status: Mapped[str] = mapped_column(String(32), default="needs_review", index=True)
    duplicate_status: Mapped[str] = mapped_column(String(32), default="not_reviewed", index=True)
    category_source: Mapped[str] = mapped_column(String(32), default="uncategorized")
    categorization_rule_id: Mapped[int | None] = mapped_column(
        ForeignKey("categorization_rules.id", ondelete="SET NULL"), default=None
    )
    category_override_id: Mapped[int | None] = mapped_column(
        ForeignKey("event_categories.id", ondelete="SET NULL"), default=None
    )
    category_overridden_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    category_overridden_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    category_override_reason: Mapped[str | None] = mapped_column(String(500), default=None)

    # Extracted venue/address/coordinates above remain immutable source values.
    # These separate fields are the narrowly scoped public-display correction.
    corrected_venue: Mapped[str | None] = mapped_column(String(500), default=None)
    corrected_address: Mapped[str | None] = mapped_column(String(1000), default=None)
    corrected_latitude: Mapped[float | None] = mapped_column(Float, default=None)
    corrected_longitude: Mapped[float | None] = mapped_column(Float, default=None)
    location_corrected_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    location_corrected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    duplicate_preferred_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("events.id", ondelete="SET NULL"), default=None
    )

    city_id: Mapped[int | None] = mapped_column(
        ForeignKey("cities.id", ondelete="SET NULL"), default=None
    )
    website_id: Mapped[int | None] = mapped_column(
        ForeignKey("websites.id", ondelete="SET NULL"), default=None
    )
    category_id: Mapped[int | None] = mapped_column(
        ForeignKey("event_categories.id", ondelete="SET NULL"), default=None
    )

    city: Mapped["City | None"] = relationship(back_populates="events")
    website: Mapped["Website | None"] = relationship(back_populates="events")
    category: Mapped["EventCategory | None"] = relationship(
        back_populates="events", foreign_keys=[category_id]
    )
    category_override: Mapped["EventCategory | None"] = relationship(
        foreign_keys=[category_override_id]
    )
    categorization_rule: Mapped["CategorizationRule | None"] = relationship()

    @property
    def effective_category(self) -> "EventCategory | None":
        return self.category_override or self.category

    @property
    def public_venue(self) -> str | None:
        return self.corrected_venue if self.corrected_venue is not None else self.venue

    @property
    def public_address(self) -> str | None:
        return self.corrected_address if self.corrected_address is not None else self.address

    @property
    def public_latitude(self) -> float | None:
        return self.corrected_latitude if self.corrected_latitude is not None else self.latitude

    @property
    def public_longitude(self) -> float | None:
        return self.corrected_longitude if self.corrected_longitude is not None else self.longitude
