"""Rate-limiting boundary for public registration.

This is a **development-safe placeholder only** — it is not a production-grade
rate limiter:
- State is an in-process dict. It resets on every restart and is not shared
  across multiple worker processes or instances (e.g. behind a load balancer
  or with `uvicorn --workers N`, each worker has its own independent counter).
- Keyed by client IP address only, which is trivially spoofable or shared
  (NAT, corporate proxies, mobile carriers) — it does not stop a distributed
  or persistent attempt.
- No persistence and only a simple time-window filter for cleanup — the
  dict can grow unbounded in memory over a long-running process that sees
  many distinct IPs.

For real deployment, replace this module's internals with a shared store
(e.g. Redis, or a database-backed counter) behind the same
`check_registration_rate_limit` function signature, called from the same
place in `app.routers.registration` — the rest of the registration flow
doesn't need to change.
"""

import time
from collections import defaultdict

from app.config import get_settings
from app.core.exceptions import AppError

_WINDOW_SECONDS = 3600

_attempts_by_ip: dict[str, list[float]] = defaultdict(list)


def check_registration_rate_limit(ip_address: str | None) -> None:
    """Raise AppError(429) if this IP has attempted registration too many
    times in the last hour. Best-effort only — see module docstring."""
    if not ip_address:
        return

    settings = get_settings()
    limit = settings.registration_rate_limit_per_hour
    now = time.monotonic()

    attempts = _attempts_by_ip[ip_address]
    attempts[:] = [t for t in attempts if now - t < _WINDOW_SECONDS]

    if len(attempts) >= limit:
        raise AppError(
            "Too many registration attempts from this address. Please try again later.",
            status_code=429,
        )

    attempts.append(now)
