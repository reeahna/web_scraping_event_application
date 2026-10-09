from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.base import TimestampMixin

if TYPE_CHECKING:
    from app.models.categorization_rule import CategorizationRule
    from app.models.event import Event


class EventCategory(Base, TimestampMixin):
    __tablename__ = "event_categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    slug: Mapped[str] = mapped_column(String(100), unique=True)
    description: Mapped[str | None] = mapped_column(String(500), default=None)
    display_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    events: Mapped[list["Event"]] = relationship(
        back_populates="category", foreign_keys="Event.category_id"
    )
    categorization_rules: Mapped[list["CategorizationRule"]] = relationship(
        back_populates="category"
    )
