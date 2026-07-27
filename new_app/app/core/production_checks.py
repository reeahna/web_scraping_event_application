"""Production-readiness assertions (Phase 18).

The master plan states production is NOT complete while it still depends on
SQLite, in-process-only rate limiting, dev cookies, or other development
defaults. This module turns that into a concrete, testable checklist rather
than a claim. It never mutates anything — it reports blockers so `/health/ready`
and operators can see exactly what remains before a real deployment.

It intentionally does not *perform* deployment; it only evaluates the running
configuration.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReadinessIssue:
    code: str
    message: str
    severity: str  # "blocker" | "warning"


def production_blockers(settings) -> list[ReadinessIssue]:
    """Return the production-readiness issues for the given settings. In a
    non-production environment this is expected to be non-empty (dev defaults);
    the list is what `is_production_ready` and the readiness endpoint report."""
    issues: list[ReadinessIssue] = []
    db_url = (settings.database_url or "").lower()

    if db_url.startswith("sqlite"):
        issues.append(ReadinessIssue(
            "sqlite_database",
            "Uses SQLite; production requires PostgreSQL (set DATABASE_URL).",
            "blocker",
        ))
    if not settings.cookie_secure:
        issues.append(ReadinessIssue(
            "insecure_cookies", "cookie_secure is off; set it True behind HTTPS.", "blocker",
        ))
    if not settings.behind_https:
        issues.append(ReadinessIssue(
            "no_https", "behind_https is off; production must terminate TLS.", "blocker",
        ))
    if not settings.trusted_hosts:
        issues.append(ReadinessIssue(
            "no_trusted_hosts",
            "trusted_hosts is empty; set it to enable host-header validation.",
            "blocker",
        ))
    if settings.rate_limit_backend == "memory":
        issues.append(ReadinessIssue(
            "in_process_rate_limit",
            "rate_limit_backend is in-process 'memory'; use a shared store in "
            "production (redis/database).",
            "blocker",
        ))
    if settings.local_login_enabled and settings.registration_enabled:
        issues.append(ReadinessIssue(
            "open_local_registration",
            "Local login and public registration are both on; review before "
            "production exposure.",
            "warning",
        ))
    return issues


def is_production_ready(settings) -> bool:
    return not any(i.severity == "blocker" for i in production_blockers(settings))
