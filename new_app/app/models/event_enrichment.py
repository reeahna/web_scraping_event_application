"""AI event enrichment suggestions (Phase 17).

A suggestion is advisory only: it is stored here and never written into an
event's authoritative fields. The unique (event_id, prompt_version,
input_hash) is the cache key — identical input under the same prompt version is
computed once. AI never approves, activates, publishes, deletes, or overrides
validation; a human applies a suggestion (e.g. a category) as a separate,
audited action.
"""

from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import TimestampMixin


class EventEnrichment(Base, TimestampMixin):
    __tablename__ = "event_enrichments"
    __table_args__ = (
        UniqueConstraint(
            "event_id", "prompt_version", "input_hash", name="uq_event_enrichment_cache"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), index=True
    )
    prompt_version: Mapped[str] = mapped_column(String(16))
    input_hash: Mapped[str] = mapped_column(String(64), index=True)
    provider: Mapped[str] = mapped_column(String(64))
    # suggested | applied | rejected
    status: Mapped[str] = mapped_column(String(16), default="suggested")

    category_suggestion: Mapped[str | None] = mapped_column(String(255), default=None)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    summary: Mapped[str | None] = mapped_column(Text, default=None)
    audience: Mapped[str | None] = mapped_column(String(255), default=None)
    family_friendly: Mapped[bool | None] = mapped_column(Boolean, default=None)
    extra: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
