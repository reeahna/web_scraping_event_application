from sqlalchemy.orm import Session

from app.models.event_provenance import EventProvenance


def create_event_provenance(
    db: Session,
    *,
    event_id: int,
    extraction_run_id: int | None,
    website_id: int,
    source_page: str,
    extraction_pattern: str,
    pattern_version: str,
    raw_record_hash: str,
    source_response_hash: str,
    field_source_paths: dict[str, str],
    transformation_history: list[str],
) -> EventProvenance:
    provenance = EventProvenance(
        event_id=event_id,
        extraction_run_id=extraction_run_id,
        website_id=website_id,
        source_page=source_page,
        extraction_pattern=extraction_pattern,
        pattern_version=pattern_version,
        raw_record_hash=raw_record_hash,
        source_response_hash=source_response_hash,
        field_source_paths=field_source_paths,
        transformation_history=transformation_history,
    )
    db.add(provenance)
    db.commit()
    db.refresh(provenance)
    return provenance


def list_provenance_for_event(db: Session, event_id: int) -> list[EventProvenance]:
    return (
        db.query(EventProvenance)
        .filter(EventProvenance.event_id == event_id)
        .order_by(EventProvenance.created_at.desc())
        .all()
    )


def get_latest_provenance_for_event(db: Session, event_id: int) -> EventProvenance | None:
    return (
        db.query(EventProvenance)
        .filter(EventProvenance.event_id == event_id)
        .order_by(EventProvenance.created_at.desc())
        .first()
    )
