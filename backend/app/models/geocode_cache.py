"""Geocode cache (Phase 11).

One row per normalized address, keyed by a hash of that address, so the same
address is never sent to the provider twice. Storing the hash (not just the
raw text) keeps the unique key short and index-friendly. A negative result
(the provider found nothing) is cached too, as ``found = False``, so a
hopeless address is not retried on every pass.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import TimestampMixin


class GeocodeCache(Base, TimestampMixin):
    __tablename__ = "geocode_cache"

    id: Mapped[int] = mapped_column(primary_key=True)
    address_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    normalized_address: Mapped[str] = mapped_column(String(1000))
    found: Mapped[bool] = mapped_column(Boolean, default=True)
    latitude: Mapped[float | None] = mapped_column(Float, default=None)
    longitude: Mapped[float | None] = mapped_column(Float, default=None)
    provider: Mapped[str] = mapped_column(String(64))
    display_name: Mapped[str | None] = mapped_column(String(1000), default=None)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
