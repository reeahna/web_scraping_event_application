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


def should_create_new_report(db: Session, website_id: int, fingerprint: str) -> bool:
    """Avoids duplicate unchanged reports: only true if the website has no
    prior report, or its latest report's fingerprint differs."""
    latest = get_latest_report_for_website(db, website_id)
    return latest is None or latest.fingerprint != fingerprint


def create_unsupported_site_report(
    db: Session, data: UnsupportedReportData
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
    )
    db.add(report)
    db.commit()
    db.refresh(report)
    return report


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
