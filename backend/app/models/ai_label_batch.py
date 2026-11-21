from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String, false
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import utcnow


class AiLabelBatch(Base):
    """One request to the AI labeling provider: what was sent, what came back,
    and what the provider said about its rate limits. Written by the scheduler's
    labeling drain (app.services.ai_categorization) so token use and the limit
    that actually stops labeling can be read back later. Never holds an API key
    or a request body."""

    __tablename__ = "ai_label_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    provider: Mapped[str] = mapped_column(String(16))
    model: Mapped[str] = mapped_column(String(100))
    # Sent to the backup model: its labels are provisional, not final.
    provisional: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    # "ok", "rate_limited", "unavailable" (5xx or network) or "error".
    outcome: Mapped[str] = mapped_column(String(16))
    http_status: Mapped[int | None] = mapped_column(Integer, default=None)
    duration_ms: Mapped[int | None] = mapped_column(Integer, default=None)
    events_submitted: Mapped[int] = mapped_column(Integer, default=0)
    events_labeled: Mapped[int] = mapped_column(Integer, default=0)
    # The model answered but left the event out or named no valid category.
    events_skipped: Mapped[int] = mapped_column(Integer, default=0)
    # The whole request failed, so no event in it got an answer.
    events_failed: Mapped[int] = mapped_column(Integer, default=0)
    events_hidden: Mapped[int] = mapped_column(Integer, default=0)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, default=None)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, default=None)
    total_tokens: Mapped[int | None] = mapped_column(Integer, default=None)
    cached_tokens: Mapped[int | None] = mapped_column(Integer, default=None)
    reasoning_tokens: Mapped[int | None] = mapped_column(Integer, default=None)
    # From the x-ratelimit-* headers: Groq's requests figures are per day and
    # its tokens figures per minute.
    limit_requests: Mapped[int | None] = mapped_column(Integer, default=None)
    remaining_requests: Mapped[int | None] = mapped_column(Integer, default=None)
    reset_requests: Mapped[str | None] = mapped_column(String(32), default=None)
    limit_tokens: Mapped[int | None] = mapped_column(Integer, default=None)
    remaining_tokens: Mapped[int | None] = mapped_column(Integer, default=None)
    reset_tokens: Mapped[str | None] = mapped_column(String(32), default=None)
    retry_after_seconds: Mapped[float | None] = mapped_column(Float, default=None)
    # Which limit a 429 names ("TPD", "TPM", "RPD", "RPM"), with the numbers
    # the provider gave for it.
    limit_kind: Mapped[str | None] = mapped_column(String(8), default=None)
    limit_value: Mapped[int | None] = mapped_column(Integer, default=None)
    limit_used: Mapped[int | None] = mapped_column(Integer, default=None)
    limit_requested: Mapped[int | None] = mapped_column(Integer, default=None)
    error: Mapped[str | None] = mapped_column(String(300), default=None)
