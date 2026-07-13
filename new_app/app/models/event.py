from datetime import date, datetime, time
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, String, Text, Time
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.base import TimestampMixin

if TYPE_CHECKING:
    from app.models.city import City
    from app.models.event_category import EventCategory
    from app.models.website import Website


class Event(Base, TimestampMixin):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)

    title: Mapped[str] = mapped_column(String(500))
    canonical_url: Mapped[str] = mapped_column(String(2000), index=True)
    source: Mapped[str] = mapped_column(String(255))
    # Source-provided ID, when available — more reliable than the URL for identifying
    # the same event across re-scrapes of a site that reuses/rewrites URLs.
    external_source_id: Mapped[str | None] = mapped_column(String(255), index=True, default=None)
    # Reserved for a future content-based dedup hash (title+venue+start_date, etc.);
    # not populated or enforced unique yet — deliberately left for a later phase.
    fingerprint: Mapped[str | None] = mapped_column(String(64), index=True, default=None)

    description: Mapped[str | None] = mapped_column(Text, default=None)

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
    category: Mapped["EventCategory | None"] = relationship(back_populates="events")
