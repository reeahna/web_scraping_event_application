"""Provider abstraction, factory, budget guard, and circuit breaker.

There are two adapters and no network code:

* `DisabledAIProvider` — the default. Always unavailable; `suggest()` returns
  a not-ok result and consumes no budget. This is what makes "the whole app
  works with AI disabled" true by construction.
* `EchoAIProvider` — a deterministic in-process adapter used by tests and by
  development to exercise the whole suggestion pipeline without a real
  provider. It returns a caller-injected canned suggestion. It never opens a
  socket.

A real network adapter would be added here, but only behind an explicitly
configured key; development and tests never supply one, so no automated code
path can reach a third party.

Budget and health live in a process-local `_UsageTracker`. It is not durable
across restarts — documented, not hidden — and is deliberately conservative:
it refuses a call the moment a limit is reached rather than after.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, date, datetime

from app.config import get_settings
from app.services.ai.types import (
    AISuggestionRequest,
    AISuggestionResult,
    AIUsageStatus,
)


class AIProviderError(Exception):
    """A provider call failed in a way worth recording (timeout, bad shape)."""


class AIProviderUnavailable(AIProviderError):
    """The provider is disabled, over budget, or tripped its breaker. Callers
    treat this as 'no suggestion available', never as a hard error."""


class _UsageTracker:
    """Process-local counters and circuit breaker.

    Not durable: a restart resets the counts. For a single-process
    development/test deployment that is acceptable and clearly stated; a
    production deployment would back this with a shared store, which is out of
    scope here.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._day: date | None = None
        self._month: tuple[int, int] | None = None
        self._daily_used = 0
        self._monthly_used = 0
        self._consecutive_failures = 0
        self._cooldown_until: datetime | None = None

    def _roll(self, now: datetime) -> None:
        if self._day != now.date():
            self._day = now.date()
            self._daily_used = 0
        month = (now.year, now.month)
        if self._month != month:
            self._month = month
            self._monthly_used = 0

    def snapshot(self, now: datetime) -> tuple[int, int, int, bool]:
        with self._lock:
            self._roll(now)
            cooldown = self._cooldown_until is not None and now < self._cooldown_until
            return (
                self._daily_used,
                self._monthly_used,
                self._consecutive_failures,
                cooldown,
            )

    def can_spend(self, now: datetime, *, daily_limit: int, monthly_limit: int) -> str | None:
        with self._lock:
            self._roll(now)
            if self._cooldown_until is not None and now < self._cooldown_until:
                return "provider is in a failure cooldown"
            if self._daily_used >= daily_limit:
                return "daily AI request limit reached"
            if self._monthly_used >= monthly_limit:
                return "monthly AI request limit reached"
            return None

    def record_success(self, now: datetime, requests: int) -> None:
        with self._lock:
            self._roll(now)
            self._daily_used += requests
            self._monthly_used += requests
            self._consecutive_failures = 0
            self._cooldown_until = None

    def record_failure(
        self, now: datetime, requests: int, *, threshold: int, cooldown_seconds: int
    ) -> None:
        from datetime import timedelta

        with self._lock:
            self._roll(now)
            self._daily_used += requests
            self._monthly_used += requests
            self._consecutive_failures += 1
            if self._consecutive_failures >= threshold:
                self._cooldown_until = now + timedelta(seconds=cooldown_seconds)


_TRACKER = _UsageTracker()


def _now() -> datetime:
    return datetime.now(UTC)


class DisabledAIProvider:
    name = "disabled"

    def available(self) -> bool:
        return False

    def suggest(self, request: AISuggestionRequest) -> AISuggestionResult:
        return AISuggestionResult(
            ok=False,
            provider=self.name,
            error="the AI configuration assistant is disabled",
            requests_used=0,
        )


class EchoAIProvider:
    """Deterministic, in-process. Returns whatever suggestion the test/dev
    caller injected via `set_canned_suggestion`. No network, ever."""

    name = "echo"

    def __init__(self) -> None:
        self._canned: Callable[[AISuggestionRequest], dict] | dict | None = None
        self._raise: Exception | None = None

    def set_canned_suggestion(self, value: Callable[[AISuggestionRequest], dict] | dict) -> None:
        self._canned = value
        self._raise = None

    def set_failure(self, exc: Exception) -> None:
        self._raise = exc

    def available(self) -> bool:
        return True

    def suggest(self, request: AISuggestionRequest) -> AISuggestionResult:
        settings = get_settings()
        now = _now()
        blocked = _TRACKER.can_spend(
            now,
            daily_limit=settings.ai_daily_request_limit,
            monthly_limit=settings.ai_monthly_request_limit,
        )
        if blocked is not None:
            raise AIProviderUnavailable(blocked)

        if self._raise is not None:
            _TRACKER.record_failure(
                now,
                1,
                threshold=settings.ai_failure_threshold,
                cooldown_seconds=settings.ai_cooldown_seconds,
            )
            raise AIProviderError(str(self._raise))

        suggestion = self._canned(request) if callable(self._canned) else self._canned
        _TRACKER.record_success(now, 1)
        return AISuggestionResult(
            ok=suggestion is not None,
            suggestion=suggestion,
            provider=self.name,
            model=settings.ai_model,
            requests_used=1,
        )


# The echo provider is a singleton so a test can inject a canned response and
# then invoke the whole pipeline, which resolves the same instance.
_ECHO = EchoAIProvider()
_DISABLED = DisabledAIProvider()


def get_ai_provider():
    settings = get_settings()
    if not settings.ai_enabled or settings.ai_provider == "disabled":
        return _DISABLED
    if settings.ai_provider == "echo":
        return _ECHO
    # An unrecognized or network provider name with no safe adapter falls
    # back to disabled rather than erroring — fail closed.
    return _DISABLED


def usage_status() -> AIUsageStatus:
    settings = get_settings()
    provider = get_ai_provider()
    now = _now()
    daily, monthly, failures, cooldown = _TRACKER.snapshot(now)
    healthy = provider.available() and not cooldown
    notes: list[str] = []
    if not settings.ai_enabled:
        notes.append("AI is disabled; deterministic inference is the only configuration source")
    if cooldown:
        notes.append("provider is in a failure cooldown")
    return AIUsageStatus(
        enabled=settings.ai_enabled and provider.available(),
        provider=provider.name,
        healthy=healthy,
        daily_used=daily,
        daily_limit=settings.ai_daily_request_limit,
        monthly_used=monthly,
        monthly_limit=settings.ai_monthly_request_limit,
        consecutive_failures=failures,
        cooldown_active=cooldown,
        notes=tuple(notes),
    )


def reset_usage_for_tests() -> None:
    """Test-only: clears the process-local counters between tests."""
    global _TRACKER
    _TRACKER = _UsageTracker()
    _ECHO._canned = None
    _ECHO._raise = None
