from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.extraction.unsupported import UnsupportedReportData
from app.models.unsupported_site_report import UnsupportedSiteReport


def get_latest_report_for_website(db: Session, website_id: int) -> UnsupportedSiteReport | None:
    return (
        db.query(UnsupportedSiteReport)
        .filter(UnsupportedSiteReport.website_id == website_id)
        .order_by(UnsupportedSiteReport.created_at.desc())
        .first()
    )


def get_report(db: Session, report_id: int) -> UnsupportedSiteReport | None:
    return db.get(UnsupportedSiteReport, report_id)


def should_create_new_report(db: Session, website_id: int, fingerprint: str) -> bool:
    """Avoids duplicate unchanged reports: only true if the website has no
    prior report, or its latest report's fingerprint differs."""
    latest = get_latest_report_for_website(db, website_id)
    return latest is None or latest.fingerprint != fingerprint


def create_unsupported_site_report(
    db: Session, data: UnsupportedReportData, *, extraction_run_id: int | None = None
) -> UnsupportedSiteReport:
    report = UnsupportedSiteReport(
        website_id=data.website_id,
        submitted_url=data.submitted_url,
        final_url=data.final_url,
        http_status=data.http_status,
        page_title=data.page_title,
        detected_platform_evidence=data.detected_platform_evidence,
        available_detector_results=data.available_detector_results,
        discovered_endpoints=data.discovered_endpoints,
        browser_required=data.browser_required,
        json_ld_presence=data.json_ld_presence,
        pagination_indicators=data.pagination_indicators,
        access_denied_or_challenge_detected=data.access_denied_or_challenge_detected,
        failure_reason=data.failure_reason,
        fingerprint=data.fingerprint,
        latest_extraction_run_id=extraction_run_id,
    )
    db.add(report)
    db.commit()
    db.refresh(report)
    return report


def record_report_occurrence(
    db: Session, website_id: int, fingerprint: str, *, run_id: int | None = None
) -> UnsupportedSiteReport | None:
    """An unchanged (same-fingerprint) repeat of the latest report bumps
    occurrence tracking on that row instead of creating a new one — the
    other half of the dedup logic should_create_new_report only decides the
    "skip creation" side of."""
    latest = get_latest_report_for_website(db, website_id)
    if latest is None or latest.fingerprint != fingerprint:
        return None
    latest.occurrence_count += 1
    latest.last_seen_at = datetime.now(UTC)
    if run_id is not None:
        latest.latest_extraction_run_id = run_id
    db.commit()
    db.refresh(latest)
    return latest


def list_reports_for_website(
    db: Session, website_id: int, *, limit: int = 20
) -> list[UnsupportedSiteReport]:
    return (
        db.query(UnsupportedSiteReport)
        .filter(UnsupportedSiteReport.website_id == website_id)
        .order_by(UnsupportedSiteReport.created_at.desc())
        .limit(limit)
        .all()
    )


def list_reports(
    db: Session,
    *,
    status: str | None = None,
    city_id: int | None = None,
    website_id: int | None = None,
    browser_required: bool | None = None,
    page: int = 1,
    per_page: int = 20,
) -> tuple[list[UnsupportedSiteReport], int]:
    from app.models.website import Website

    query = db.query(UnsupportedSiteReport)
    if status:
        query = query.filter(UnsupportedSiteReport.status == status)
    if website_id is not None:
        query = query.filter(UnsupportedSiteReport.website_id == website_id)
    if browser_required is not None:
        query = query.filter(UnsupportedSiteReport.browser_required.is_(browser_required))
    if city_id is not None:
        query = query.join(Website, Website.id == UnsupportedSiteReport.website_id).filter(
            Website.city_id == city_id
        )

    total = query.count()
    page = max(page, 1)
    items = (
        query.order_by(UnsupportedSiteReport.created_at.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )
    return items, total


def count_unresolved_reports(db: Session) -> int:
    from app.core.report_status import DISMISSED, RESOLVED

    return (
        db.query(UnsupportedSiteReport)
        .filter(UnsupportedSiteReport.status.notin_((RESOLVED, DISMISSED)))
        .count()
    )


def assign_report(
    db: Session, report: UnsupportedSiteReport, *, assigned_user_id: int | None
) -> UnsupportedSiteReport:
    report.assigned_user_id = assigned_user_id
    db.commit()
    db.refresh(report)
    return report


def add_report_note(
    db: Session, report: UnsupportedSiteReport, *, note: str
) -> UnsupportedSiteReport:
    existing = report.admin_notes or ""
    report.admin_notes = f"{existing}\n{note}".strip() if existing else note
    db.commit()
    db.refresh(report)
    return report


def change_report_status(
    db: Session,
    report: UnsupportedSiteReport,
    *,
    status: str,
    resolved_by_user_id: int | None = None,
) -> UnsupportedSiteReport:
    from app.core.report_status import DISMISSED, RESOLVED

    report.status = status
    if status in (RESOLVED, DISMISSED):
        report.resolved_at = datetime.now(UTC)
        report.resolved_by_user_id = resolved_by_user_id
    else:
        report.resolved_at = None
        report.resolved_by_user_id = None
    db.commit()
    db.refresh(report)
    return report
