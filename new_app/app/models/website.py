from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

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
    base_url: Mapped[str] = mapped_column(String(2000))
    city_id: Mapped[int | None] = mapped_column(
        ForeignKey("cities.id", ondelete="SET NULL"), default=None
    )
    requires_js: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Prerequisite (along with events) for safely deleting the website's city.
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    city: Mapped["City | None"] = relationship(back_populates="websites")
    events: Mapped[list["Event"]] = relationship(back_populates="website")
