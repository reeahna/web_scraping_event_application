"""Visitor log: who visits the public site, day by day, without cookies and
without storing anyone's IP address.

A visitor is counted by a hash of (daily salt, IP, user agent). The salt is
random, lives in `visit_salts` for the current day only and is deleted once
the day is over, so yesterday's hashes can never be recomputed from an IP and
a visitor can't be followed from one day to the next. Rows are kept for a
limited time (`VISIT_RETENTION_DAYS`). See app.services.site_visits.
"""

from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import utcnow


class SiteVisitorDay(Base):
    """One row per visitor per day: how many pages they opened, which towns
    they looked at, where they came from and, if signed in, who they are."""

    __tablename__ = "site_visitor_days"
    __table_args__ = (UniqueConstraint("day", "visitor_hash", name="uq_site_visitor_day"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    visitor_hash: Mapped[str] = mapped_column(String(32))
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True, default=None
    )
    page_views: Mapped[int] = mapped_column(Integer, default=0)
    device: Mapped[str] = mapped_column(String(16), default="desktop")
    referrer_host: Mapped[str | None] = mapped_column(String(255), default=None)
    # Comma-separated town slugs in the order first viewed, capped (see service).
    towns: Mapped[str | None] = mapped_column(String(500), default=None)
    last_path: Mapped[str | None] = mapped_column(String(255), default=None)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SitePageDay(Base):
    """Page views per page per day."""

    __tablename__ = "site_page_days"
    __table_args__ = (UniqueConstraint("day", "path", name="uq_site_page_day"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    path: Mapped[str] = mapped_column(String(255))
    town_slug: Mapped[str | None] = mapped_column(String(120), index=True, default=None)
    views: Mapped[int] = mapped_column(Integer, default=0)


class VisitSalt(Base):
    """The current day's random salt for visitor hashes. Older rows are
    deleted, which is what makes past hashes unlinkable."""

    __tablename__ = "visit_salts"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    salt: Mapped[str] = mapped_column(String(64))
