from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.base import TimestampMixin

if TYPE_CHECKING:
    from app.models.event_category import EventCategory
    from app.models.website import Website


class CategorizationRule(Base, TimestampMixin):
    __tablename__ = "categorization_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    rule_type: Mapped[str] = mapped_column(String(32), index=True)
    category_id: Mapped[int] = mapped_column(
        ForeignKey("event_categories.id", ondelete="RESTRICT"), index=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    website_id: Mapped[int | None] = mapped_column(
        ForeignKey("websites.id", ondelete="CASCADE"), default=None, index=True
    )
    source_category_value: Mapped[str | None] = mapped_column(String(255), default=None)
    pattern: Mapped[str | None] = mapped_column(String(500), default=None)
    is_regex: Mapped[bool] = mapped_column(Boolean, default=False)
    case_sensitive: Mapped[bool] = mapped_column(Boolean, default=False)

    category: Mapped["EventCategory"] = relationship(back_populates="categorization_rules")
    website: Mapped["Website | None"] = relationship()
