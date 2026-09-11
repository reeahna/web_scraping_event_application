from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import TimestampMixin


class CategoryPhoto(Base, TimestampMixin):
    """A pool of Unsplash photos per event category, used as a relevant
    placeholder for events that have no image of their own. Populated by
    scripts/fetch_category_photos.py; each row carries the photographer credit
    Unsplash requires when a photo is displayed."""

    __tablename__ = "category_photos"

    id: Mapped[int] = mapped_column(primary_key=True)
    category_slug: Mapped[str] = mapped_column(String(100), index=True)
    unsplash_id: Mapped[str] = mapped_column(String(100), unique=True)
    image_url: Mapped[str] = mapped_column(String(1000))
    thumb_url: Mapped[str | None] = mapped_column(String(1000), default=None)
    photographer: Mapped[str] = mapped_column(String(200))
    photographer_url: Mapped[str] = mapped_column(String(500))
    source_url: Mapped[str] = mapped_column(String(500))
