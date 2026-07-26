"""Validated per-site schedule configuration.

Stored as JSON in ``Website.schedule_config``. Closed and plain-data: it
describes *when* an approved source is refreshed, never *how* — there is no
callable, expression, or command here. Every interval is floored so a
misconfiguration cannot hammer a source, and the retry policy is bounded.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

# A hard floor on how often a source may be refreshed, independent of what a
# form submits — protects a third-party site from being polled aggressively.
MIN_INTERVAL_MINUTES = 15
MAX_INTERVAL_MINUTES = 60 * 24 * 30  # 30 days


class ScheduleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    interval_minutes: int = Field(default=1440, ge=MIN_INTERVAL_MINUTES, le=MAX_INTERVAL_MINUTES)
    # Random spread added to each scheduled fire so many sources don't stampede
    # at the same instant. Bounded and optional.
    jitter_seconds: int = Field(default=60, ge=0, le=3600)
    # Per-run retry policy. Bounded exponential backoff; after these retries a
    # run is left failed and the site's consecutive-failure count advances
    # toward the FAILING threshold (handled by the extraction health logic).
    max_retries: int = Field(default=2, ge=0, le=5)
    retry_backoff_seconds: int = Field(default=60, ge=1, le=3600)
    retry_backoff_max_seconds: int = Field(default=1800, ge=1, le=86_400)

    def backoff_for_attempt(self, attempt: int) -> int:
        """Exponential backoff (seconds) for retry `attempt` (1-based), capped.
        Deterministic — no randomness, so it is testable and reproducible."""
        if attempt < 1:
            attempt = 1
        raw = self.retry_backoff_seconds * (2 ** (attempt - 1))
        return min(raw, self.retry_backoff_max_seconds)


def parse_schedule_config(raw: dict | None) -> ScheduleConfig | None:
    """Return a validated ScheduleConfig, or None when no schedule is stored.
    Never raises on an absent config; an invalid stored config raises so the
    caller (eligibility) can treat the site as not-schedulable rather than
    silently guessing defaults."""
    if not raw:
        return None
    return ScheduleConfig.model_validate(raw)
