from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models.extraction_run import ExtractionRun


def create_extraction_run(
    db: Session,
    *,
    website_id: int,
    configuration_version: int | None,
    pattern_name: str | None,
    run_type: str,
    status: str,
    source_url: str,
    final_url: str | None,
    started_at: datetime,
    completed_at: datetime | None = None,
    events_found: int = 0,
    events_valid: int = 0,
    events_rejected: int = 0,
    events_inserted: int = 0,
    events_updated: int = 0,
    duplicates_skipped: int = 0,
    warnings: list[str] | None = None,
    error_summary: str | None = None,
    detector_evidence: dict[str, Any] | None = None,
    response_metadata: dict[str, Any] | None = None,
    initiating_user_id: int | None = None,
    correlation_id: str | None = None,
) -> ExtractionRun:
    run = ExtractionRun(
        website_id=website_id,
        configuration_version=configuration_version,
        pattern_name=pattern_name,
        run_type=run_type,
        status=status,
        source_url=source_url,
        final_url=final_url,
        events_found=events_found,
        events_valid=events_valid,
        events_rejected=events_rejected,
        events_inserted=events_inserted,
        events_updated=events_updated,
        duplicates_skipped=duplicates_skipped,
        warnings=warnings,
        error_summary=error_summary,
        detector_evidence=detector_evidence,
        response_metadata=response_metadata,
        started_at=started_at,
        completed_at=completed_at,
        initiating_user_id=initiating_user_id,
        correlation_id=correlation_id,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def list_extraction_runs_for_website(
    db: Session, website_id: int, *, limit: int = 20
) -> list[ExtractionRun]:
    return (
        db.query(ExtractionRun)
        .filter(ExtractionRun.website_id == website_id)
        .order_by(ExtractionRun.started_at.desc())
        .limit(limit)
        .all()
    )


def get_extraction_run(db: Session, run_id: int) -> ExtractionRun | None:
    return db.get(ExtractionRun, run_id)
