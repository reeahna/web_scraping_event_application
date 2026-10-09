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
    base_url = (settings.public_base_url or "").lower()
    if not base_url or "localhost" in base_url or "127.0.0.1" in base_url:
        issues.append(ReadinessIssue(
            "local_public_base_url",
            "public_base_url still points at localhost; canonical links, Open "
            "Graph URLs and the sitemap would name an unreachable host, which "
            "is worse for search engines than omitting them. Set "
            "PUBLIC_BASE_URL to the site's real address.",
            "blocker",
        ))
    elif not base_url.startswith("https://"):
        issues.append(ReadinessIssue(
            "insecure_public_base_url",
            "public_base_url is not https; canonical links would advertise "
            "the insecure address.",
            "warning",
        ))

    if settings.rate_limit_backend == "memory":
        issues.append(ReadinessIssue(
            "in_process_rate_limit",
            "rate_limit_backend is in-process 'memory'; use a shared store in "
            "production (redis/database).",
            "blocker",
        ))
    from app.services.oauth import enabled_provider_names

    if not enabled_provider_names(settings):
        issues.append(ReadinessIssue(
            "no_sign_in_provider",
            "No sign-in provider (Google, Microsoft or Facebook) has a client id and "
            "secret, so nobody can sign in.",
            "warning",
        ))
    if not settings.superadmin_email:
        issues.append(ReadinessIssue(
            "no_superadmin_email",
            "SUPERADMIN_EMAIL is not set; with sign-in by Google/Microsoft/Facebook "
            "only, nobody can be made a Super Administrator without it.",
            "warning",
        ))
    return issues


def is_production_ready(settings) -> bool:
    return not any(i.severity == "blocker" for i in production_blockers(settings))
