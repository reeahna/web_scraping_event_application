from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Float, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.base import TimestampMixin

if TYPE_CHECKING:
    from app.models.event import Event
    from app.models.website import Website


class City(Base, TimestampMixin):
    __tablename__ = "cities"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    slug: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    state_or_region: Mapped[str | None] = mapped_column(String(100), default=None)
    country: Mapped[str | None] = mapped_column(String(100), default=None)
    latitude: Mapped[float | None] = mapped_column(Float, default=None)
    longitude: Mapped[float | None] = mapped_column(Float, default=None)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    websites: Mapped[list["Website"]] = relationship(back_populates="city")
    events: Mapped[list["Event"]] = relationship(back_populates="city")
