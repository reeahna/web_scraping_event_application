"""Operational reporting (Phase 15).

Builds one read-only operational view over the whole system. Every query is
bounded, and only safe fields are surfaced: no secrets, API keys, cookies, auth
headers, OAuth tokens, provider credentials, raw source content, audit
before/after payloads, or other users' alert preferences ever appear here.
Counts and short recent lists only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.auto_onboarding_decision import AutoOnboardingDecision
from app.models.event import Event
from app.models.extraction_error import ExtractionError
from app.models.extraction_run import ExtractionRun
from app.models.notification import Notification
from app.models.onboarding_job import OnboardingJob
from app.models.unsupported_site_report import UnsupportedSiteReport
from app.models.website import Website

_RECENT = 10
_RUN_WARNING_SCAN = 200


@dataclass
class OperationalReport:
    generated_at: datetime
    counts: dict[str, int] = field(default_factory=dict)
    sources_by_status: dict[str, int] = field(default_factory=dict)
    onboarding_queue: dict[str, int] = field(default_factory=dict)
    geocoding: dict[str, int] = field(default_factory=dict)
    scheduler: dict = field(default_factory=dict)
    ai_usage: dict = field(default_factory=dict)
    recent_runs: list[dict] = field(default_factory=list)
    recent_errors: list[dict] = field(default_factory=list)
    recent_unsupported: list[dict] = field(default_factory=list)
    recent_notifications: list[dict] = field(default_factory=list)
    recent_decisions: list[dict] = field(default_factory=list)
    recent_duplicates: list[dict] = field(default_factory=list)
    recent_audits: list[dict] = field(default_factory=list)
    audit_page: int = 1
    audit_has_next: bool = False


def _status_counts(db: Session, column) -> dict[str, int]:
    rows = db.execute(select(column, func.count()).group_by(column)).all()
    return {str(value): count for value, count in rows}


def _isot(value) -> str | None:
    return value.isoformat() if value else None


def build_operational_report(
    db: Session, *, audit_page: int = 1, audit_per_page: int = 20
) -> OperationalReport:
    from app.services.ai.provider import usage_status
    from app.services.scheduler import scheduler_health

    report = OperationalReport(generated_at=datetime.now(UTC))

    report.sources_by_status = _status_counts(db, Website.onboarding_status)
    report.onboarding_queue = _status_counts(db, OnboardingJob.status)
    report.geocoding = _status_counts(db, Event.geocode_status)

    run_status = _status_counts(db, ExtractionRun.status)

    # Top-line metric cards.
    report.counts = {
        "active_sources": report.sources_by_status.get("active", 0),
        "failing_sources": report.sources_by_status.get("failing", 0),
        "needs_review_sources": report.sources_by_status.get("needs_review", 0),
        "onboarding_pending": report.onboarding_queue.get("queued", 0),
        "runs_failed": run_status.get("failed", 0) + run_status.get("blocked", 0),
        "unsupported_reports": db.scalar(select(func.count()).select_from(UnsupportedSiteReport))
        or 0,
        "validation_errors": db.scalar(select(func.count()).select_from(ExtractionError)) or 0,
        "geocoding_failed": report.geocoding.get("failed", 0),
        "geocoding_needs_review": report.geocoding.get("needs_review", 0),
        "duplicate_queue": db.scalar(
            select(func.count()).select_from(Event).where(
                Event.duplicate_status == "possible_duplicate"
            )
        ) or 0,
        "drift_proposals": db.scalar(
            select(func.count()).select_from(Notification).where(
                Notification.notification_type == "website_structure_reonboarding"
            )
        ) or 0,
    }

    # Recurrence truncations and geography exclusions live in run warnings;
    # scan a bounded window of recent runs for their markers.
    recurrence_truncations = geography_exclusions = 0
    recent_runs = list(
        db.scalars(select(ExtractionRun).order_by(ExtractionRun.id.desc()).limit(_RUN_WARNING_SCAN))
    )
    for run in recent_runs:
        for warning in run.warnings or []:
            if "recurrence_truncated" in warning or "recurrence_run_budget" in warning:
                recurrence_truncations += 1
            if warning.startswith("geographic_filter_excluded"):
                geography_exclusions += 1
    report.counts["recurrence_truncations"] = recurrence_truncations
    report.counts["geography_exclusions"] = geography_exclusions

    report.recent_runs = [
        {
            "id": r.id, "website_id": r.website_id, "pattern": r.pattern_name,
            "run_type": r.run_type, "status": r.status, "events_found": r.events_found,
            "events_valid": r.events_valid, "events_rejected": r.events_rejected,
            "started_at": _isot(r.started_at),
        }
        for r in recent_runs[:_RECENT]
    ]

    report.recent_errors = [
        {
            "id": e.id, "run_id": e.extraction_run_id, "stage": e.stage,
            "error_code": e.error_code, "message": e.safe_message,
            "created_at": _isot(e.created_at),
        }
        for e in db.scalars(
            select(ExtractionError).order_by(ExtractionError.id.desc()).limit(_RECENT)
        )
    ]

    report.recent_unsupported = [
        {
            "id": u.id, "status": getattr(u, "status", None),
            "created_at": _isot(getattr(u, "created_at", None)),
        }
        for u in db.scalars(
            select(UnsupportedSiteReport).order_by(UnsupportedSiteReport.id.desc()).limit(_RECENT)
        )
    ]

    # Notifications are already user-safe (title/message); still surface only
    # those fields, never any related payload.
    report.recent_notifications = [
        {
            "id": n.id, "type": n.notification_type, "severity": n.severity,
            "title": n.title, "created_at": _isot(getattr(n, "created_at", None)),
        }
        for n in db.scalars(select(Notification).order_by(Notification.id.desc()).limit(_RECENT))
    ]

    report.recent_decisions = [
        {
            "id": d.id, "website_id": d.website_id, "final_decision": d.final_decision,
            "detected_pattern": d.detected_pattern, "origin": d.configuration_origin,
            "created_at": _isot(getattr(d, "created_at", None)),
        }
        for d in db.scalars(
            select(AutoOnboardingDecision).order_by(AutoOnboardingDecision.id.desc()).limit(_RECENT)
        )
    ]

    report.recent_duplicates = [
        {"id": ev.id, "title": ev.title, "start_date": _isot(ev.start_date)}
        for ev in db.scalars(
            select(Event).where(Event.duplicate_status == "possible_duplicate")
            .order_by(Event.id.desc()).limit(_RECENT)
        )
    ]

    # Recent audits — action/entity/actor/detail only. The before/after state
    # payloads are deliberately NOT surfaced (they can contain configuration).
    audit_page = max(audit_page, 1)
    audit_rows = list(
        db.scalars(
            select(AuditLog).order_by(AuditLog.id.desc())
            .offset((audit_page - 1) * audit_per_page).limit(audit_per_page + 1)
        )
    )
    report.audit_has_next = len(audit_rows) > audit_per_page
    report.recent_audits = [
        {
            "id": a.id, "action": a.action, "entity_type": a.entity_type,
            "entity_id": a.entity_id, "actor_type": a.actor_type, "actor_label": a.actor_label,
            "detail": a.detail, "created_at": _isot(a.created_at),
        }
        for a in audit_rows[:audit_per_page]
    ]
    report.audit_page = audit_page

    report.scheduler = _scheduler_dict(scheduler_health(db))
    report.ai_usage = _ai_usage_dict(usage_status())
    return report


def _scheduler_dict(health) -> dict:
    return {
        "leader_holder": health.leader_holder,
        "leader_is_fresh": health.leader_is_fresh,
        "scheduled_count": health.scheduled_count,
        "running_count": health.running_count,
        "paused_count": health.paused_count,
    }


def _ai_usage_dict(status) -> dict:
    # Only counts/health — never a key or any request content.
    return {
        "provider": getattr(status, "provider", None),
        "enabled": getattr(status, "enabled", None),
        "healthy": getattr(status, "healthy", None),
        "daily_used": getattr(status, "daily_used", None),
        "monthly_used": getattr(status, "monthly_used", None),
    }
