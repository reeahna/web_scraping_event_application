from datetime import date, datetime, time
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, Time
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

    # Recurrence (Phase 8G). Populated only for events produced by the shared
    # recurrence expander. `occurrence_id` is the deterministic per-occurrence
    # identity used as the source id so a re-run matches an occurrence instead
    # of duplicating it. `is_cancelled` is set for a cancelled occurrence: the
    # event is deactivated (hidden) but never deleted, so history survives.
    occurrence_id: Mapped[str | None] = mapped_column(String(512), index=True, default=None)
    recurrence_parent_id: Mapped[str | None] = mapped_column(String(512), index=True, default=None)
    is_recurrence_parent: Mapped[bool] = mapped_column(Boolean, default=False)
    is_cancelled: Mapped[bool] = mapped_column(Boolean, default=False)

    venue: Mapped[str | None] = mapped_column(String(500), default=None)
    address: Mapped[str | None] = mapped_column(String(1000), default=None)
    image_url: Mapped[str | None] = mapped_column(String(2000), default=None)

    latitude: Mapped[float | None] = mapped_column(Float, default=None)
    longitude: Mapped[float | None] = mapped_column(Float, default=None)

    # Geocoding (Phase 11). Derived coordinates, kept separate from the
    # immutable source coordinates (latitude/longitude) and the administrator
    # correction (corrected_latitude/longitude). Status drives the async queue:
    # pending | completed | failed | skipped | needs_review.
    geocoded_latitude: Mapped[float | None] = mapped_column(Float, default=None)
    geocoded_longitude: Mapped[float | None] = mapped_column(Float, default=None)
    geocode_status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    geocode_attempts: Mapped[int] = mapped_column(Integer, default=0)
    geocode_last_error: Mapped[str | None] = mapped_column(String(500), default=None)
    geocoded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    scraped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # When visitor-facing information last changed: a source edit noticed by a
    # check, or an administrator's correction. Not the last scrape (scraped_at)
    # and not the last database write (updated_at). Set only by
    # app.services.event_freshness. NULL for events listed before this was
    # tracked, whose change history is unknown; the public page then says nothing.
    content_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Distinct from is_active: archiving is a deliberate curatorial step (a
    # prerequisite for deleting the event's city), not just a visibility toggle.
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # Defaults to "reviewed": a scraped event is trusted unless something
    # actually flags it. When every new event defaulted to "needs_review" the
    # status never distinguished anything — the genuinely suspect events (see
    # app.services.geographic_filter.geo_needs_review, the one automated
    # writer) were indistinguishable from the thousands nobody had looked at,
    # and the admin filter partitioned the list into "everything" and "nothing".
    review_status: Mapped[str] = mapped_column(String(32), default="reviewed", index=True)
    duplicate_status: Mapped[str] = mapped_column(String(32), default="not_reviewed", index=True)
    category_source: Mapped[str] = mapped_column(String(32), default="uncategorized")
    # The ai_categorizer PROMPT_VERSION that produced an "ai" category. An
    # older version means the label predates the current prompt and is queued
    # for a re-check (see app.services.ai_categorization.pending_events).
    ai_prompt_version: Mapped[str | None] = mapped_column(String(16), default=None)
    # AI label bookkeeping (app.services.ai_label_state). ai_label_status is NULL
    # until the AI has answered for the event, then "labeled"; "stale" when the
    # words the model reads have changed since, and "needs_review" after
    # AI_MAX_ATTEMPTS answers in a row gave no usable label. ai_input_hash is a
    # digest of those words at the last AI attempt, so a re-scrape can tell
    # whether they changed.
    ai_label_status: Mapped[str | None] = mapped_column(String(16), default=None, index=True)
    ai_attempts: Mapped[int] = mapped_column(Integer, default=0)
    ai_last_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    ai_last_error: Mapped[str | None] = mapped_column(String(300), default=None)
    ai_input_hash: Mapped[str | None] = mapped_column(String(64), default=None)
    # When an administrator last activated or deactivated the event by hand.
    # Automatic hiding (the AI's "keep" verdict) never overrules that decision.
    visibility_set_by_admin_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
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
        # Preference: administrator correction > immutable source > geocoded.
        if self.corrected_latitude is not None:
            return self.corrected_latitude
        if self.latitude is not None:
            return self.latitude
        return self.geocoded_latitude

    @property
    def public_longitude(self) -> float | None:
        if self.corrected_longitude is not None:
            return self.corrected_longitude
        if self.longitude is not None:
            return self.longitude
        return self.geocoded_longitude
