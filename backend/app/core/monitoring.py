"""Error monitoring with Sentry.

Everything here does nothing unless SENTRY_DSN is set: no SDK is initialised,
no network call is made, and ``report_exception`` / ``scheduler_checkin`` return
at once. That keeps development, the test suite and any deployment without a
DSN exactly as they were.

When a DSN is set, ``init_monitoring`` is called once per process (the web
server in ``app.main``, the scheduler in ``app.scheduler.__main__``) and Sentry
then receives:

* every unhandled exception in a web request (through Sentry's FastAPI
  integration, which is enabled automatically when FastAPI is installed);
* everything logged at ERROR or above, from any ``app.*`` logger;
* every failure the scheduler's background jobs catch and survive, reported
  explicitly with ``report_exception`` because those are logged as warnings
  (a failing job must never stop the scheduler, but it must still be seen);
* a check-in from the scheduler leader about once a minute, so Sentry's cron
  monitor raises an alert when the scheduler stops running at all.

To stay inside a free Sentry plan, performance tracing samples only a small
share of web requests (SENTRY_TRACES_SAMPLE_RATE) and skips health checks and
static files entirely, and the same error from the same place is sent at most
once per ``REPEAT_WINDOW_SECONDS`` per process. A failure that repeats every
minute for hours (the shape of most real outages) still opens one issue and
keeps it marked as ongoing, but spends a few events an hour, not sixty.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import traceback
from typing import Any

from app.core.logging import get_logger

logger = get_logger("monitoring")

# How long the same error (same exception type raised from the same line, or
# the same logged message) is held back after it was last sent.
REPEAT_WINDOW_SECONDS = 30 * 60
# The cron monitor the scheduler checks in to. Sentry creates it on the first
# check-in from the settings in SCHEDULER_MONITOR_CONFIG.
SCHEDULER_MONITOR_SLUG = "bulletin-scheduler"
SCHEDULER_MONITOR_CONFIG = {
    "schedule": {"type": "interval", "value": 5, "unit": "minute"},
    # Alert when no check-in has arrived for the 5 minute interval plus this
    # many minutes: long enough to ride out a deploy's leader handover.
    "checkin_margin": 10,
    "max_runtime": 1,
    "failure_issue_threshold": 1,
    "recovery_threshold": 1,
}
CHECKIN_INTERVAL_SECONDS = 60
# Requests never worth tracing: the platform's health probe hits these every
# few seconds and would use up the tracing quota on its own.
_UNTRACED_PATH_PREFIXES = ("/health", "/static", "/favicon")

_enabled = False
_lock = threading.Lock()
_last_sent: dict[str, float] = {}
_last_checkin = 0.0


def is_enabled() -> bool:
    return _enabled


def init_monitoring(settings, *, component: str) -> bool:
    """Start Sentry for this process when SENTRY_DSN is set. Returns whether it did.

    ``component`` ("web" or "scheduler") is attached to every event as a tag,
    so the two processes can be told apart in Sentry.
    """
    global _enabled
    dsn = (settings.sentry_dsn or "").strip()
    if not dsn:
        return False

    import sentry_sdk
    from sentry_sdk.integrations.logging import LoggingIntegration

    traces_rate = max(0.0, min(1.0, float(settings.sentry_traces_sample_rate or 0.0)))
    sentry_sdk.init(
        dsn=dsn,
        environment=settings.sentry_environment or settings.app_env,
        # Render sets this to the deployed commit, so each issue shows which
        # deploy introduced it.
        release=os.environ.get("RENDER_GIT_COMMIT") or None,
        integrations=[LoggingIntegration(level=logging.INFO, event_level=logging.ERROR)],
        traces_sampler=_make_traces_sampler(traces_rate if component == "web" else 0.0),
        before_send=_before_send,
        # Personal data stays off: the SDK's default sends no IP addresses,
        # cookies or signed-in user details, and nothing here turns that on.
    )
    sentry_sdk.get_global_scope().set_tag("component", component)
    _enabled = True
    logger.info("error monitoring on (Sentry, component=%s)", component)
    return True


def report_exception(exc: BaseException, *, job: str) -> None:
    """Send an exception that was caught and handled (a background job failure)."""
    if not _enabled:
        return
    try:
        import sentry_sdk

        with sentry_sdk.new_scope() as scope:
            scope.set_tag("job", job)
            sentry_sdk.capture_exception(exc)
    except Exception:  # noqa: BLE001 - reporting must never break the caller
        logger.debug("could not report exception to Sentry", exc_info=True)


def scheduler_checkin() -> None:
    """Tell Sentry the scheduler is alive. Sent at most once a minute."""
    global _last_checkin
    if not _enabled:
        return
    now = time.monotonic()
    if _last_checkin and now - _last_checkin < CHECKIN_INTERVAL_SECONDS:
        return
    _last_checkin = now
    try:
        from sentry_sdk.crons import capture_checkin
        from sentry_sdk.crons.consts import MonitorStatus

        capture_checkin(
            monitor_slug=SCHEDULER_MONITOR_SLUG,
            status=MonitorStatus.OK,
            monitor_config=SCHEDULER_MONITOR_CONFIG,
        )
    except Exception:  # noqa: BLE001 - reporting must never break the caller
        logger.debug("could not send scheduler check-in to Sentry", exc_info=True)


def _make_traces_sampler(rate: float):
    def sampler(sampling_context: dict[str, Any]) -> float:
        if rate <= 0:
            return 0.0
        scope = sampling_context.get("asgi_scope") or {}
        path = scope.get("path") or ""
        if path.startswith(_UNTRACED_PATH_PREFIXES):
            return 0.0
        return rate

    return sampler


def _repeat_key(event: dict[str, Any], hint: dict[str, Any]) -> str:
    tags = event.get("tags") or {}
    job = tags.get("job", "") if isinstance(tags, dict) else ""
    exc_info = hint.get("exc_info")
    if exc_info and exc_info[0] is not None:
        exc_type, _, tb = exc_info
        frames = traceback.extract_tb(tb) if tb is not None else []
        where = f"{frames[-1].filename}:{frames[-1].lineno}" if frames else ""
        return f"exc|{job}|{exc_type.__module__}.{exc_type.__qualname__}|{where}"
    logentry = event.get("logentry") or {}
    message = logentry.get("message") or event.get("message") or ""
    return f"log|{job}|{event.get('logger', '')}|{message}"


def _before_send(event: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any] | None:
    """Drop an event when the same error was sent within the repeat window."""
    key = _repeat_key(event, hint)
    now = time.monotonic()
    with _lock:
        last = _last_sent.get(key)
        if last is not None and now - last < REPEAT_WINDOW_SECONDS:
            return None
        _last_sent[key] = now
        if len(_last_sent) > 1000:
            cutoff = now - REPEAT_WINDOW_SECONDS
            for stale in [k for k, t in _last_sent.items() if t < cutoff]:
                del _last_sent[stale]
    return event


def _reset_for_tests() -> None:
    global _enabled, _last_checkin
    _enabled = False
    _last_checkin = 0.0
    with _lock:
        _last_sent.clear()
