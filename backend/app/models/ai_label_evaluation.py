from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import utcnow


class AiLabelEvaluation(Base):
    """A side-by-side test of AI labeling setups (model, description length,
    prompt layout, batch size) on one fixed sample of events, run a request
    at a time by the scheduler (app.services.ai_label_eval). It only reads
    events: no label it produces is ever written to an event.

    JSON columns are replaced whole on every change, never mutated in place."""

    __tablename__ = "ai_label_evaluations"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    # "running", "done", "cancelled" or "failed".
    status: Mapped[str] = mapped_column(String(16), default="running", index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # [{"key", "model", "description_chars", "layout", "batch_size"}, ...]; the
    # first is the baseline the others are compared with.
    configs: Mapped[list[Any]] = mapped_column(JSON, default=list)
    # The sampled events' inputs, frozen when the evaluation started so every
    # setup sees exactly the same words: [{"id", "title", "description",
    # "source_category", "venue", "buckets", "stored_category",
    # "override_category"}, ...].
    sample: Mapped[list[Any]] = mapped_column(JSON, default=list)
    # Per config key: {"labels": {event_id: [category, keep]}, "done": [chunk
    # indexes], "tokens": {...}, "requests": n, "errors": [...]}, plus
    # "_cooldowns": {model: iso time} for models that are rate limited.
    progress: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    report: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    error: Mapped[str | None] = mapped_column(String(300), default=None)
    requests_made: Mapped[int] = mapped_column(Integer, default=0)
