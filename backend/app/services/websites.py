from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.core.exceptions import AppError
from app.core.onboarding import ACTIVE, ARCHIVED, can_transition
from app.models.event import Event
from app.models.website import Website


def transition_website(db: Session, website: Website, target_status: str) -> Website:
    """Move a website to `target_status`, keeping is_active/archived_at in sync.
    Raises AppError(409) if the transition isn't allowed from the current state."""
    if not can_transition(website.onboarding_status, target_status):
        raise AppError(
            f"Cannot move website from '{website.onboarding_status}' to '{target_status}'",
            status_code=409,
        )

    if target_status == ACTIVE:
        if not website.approved_pattern or website.active_configuration_version is None:
            raise AppError(
                "Website cannot be activated without an approved configuration.",
                status_code=409,
            )
        if website.city_id is None or website.city is None or not website.city.is_active:
            raise AppError(
                "Website cannot be activated unless it is assigned to an active city.",
                status_code=409,
            )

    website.onboarding_status = target_status
    website.is_active = target_status == ACTIVE
    if target_status == ARCHIVED:
        website.archived_at = datetime.now(UTC)
    db.commit()
    db.refresh(website)

    # Becoming approved+active with NO schedule at all means the source would
    # silently never import. Give it the default automatic schedule (daily),
    # but never overwrite an existing one — a schedule an administrator
    # explicitly disabled stays disabled across reactivation. Import lazily to
    # avoid a service import cycle.
    if target_status == ACTIVE:
        from app.services.schedule_admin import ensure_default_schedule

        if ensure_default_schedule(website):
            db.commit()
            db.refresh(website)

    # Keep the durable scheduler job in step with the lifecycle: leaving ACTIVE
    # (deactivation, failing, archive) pauses future runs immediately; entering
    # ACTIVE clears the pause. Events and history are untouched.
    from app.services.scheduler import set_paused

    set_paused(db, website.id, paused=target_status != ACTIVE)
    return website


@dataclass
class WebsiteDeletionImpact:
    total_events: int
    archived_events: int

    @property
    def unarchived_events(self) -> int:
        return self.total_events - self.archived_events

    @property
    def can_delete(self) -> bool:
        return self.unarchived_events == 0


def get_deletion_impact(db: Session, website: Website) -> WebsiteDeletionImpact:
    total_events = db.query(Event).filter(Event.website_id == website.id).count()
    archived_events = (
        db.query(Event)
        .filter(Event.website_id == website.id, Event.archived_at.isnot(None))
        .count()
    )
    return WebsiteDeletionImpact(total_events=total_events, archived_events=archived_events)
