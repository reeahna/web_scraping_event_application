from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.onboarding import DRAFT
from app.models.website import Website
from app.schemas.website import WebsiteCreate, WebsiteUpdate


def create_website(db: Session, data: WebsiteCreate) -> Website:
    """A new website always starts inactive, in DRAFT — never auto-activated."""
    website = Website(**data.model_dump(), is_active=False, onboarding_status=DRAFT)
    db.add(website)
    db.commit()
    db.refresh(website)
    return website


def update_website(db: Session, website: Website, data: WebsiteUpdate) -> Website:
    """Updates only the core site-identity fields — never is_active/
    onboarding_status (app.services.websites.transition_website) and never
    configuration/proposed_pattern/approved_pattern
    (app.services.website_configuration), which this schema no longer
    carries at all."""
    for field, value in data.model_dump().items():
        setattr(website, field, value)
    db.commit()
    db.refresh(website)
    return website


def get_website(db: Session, website_id: int) -> Website | None:
    return db.get(Website, website_id)


def list_websites(db: Session, *, active_only: bool = True) -> list[Website]:
    query = db.query(Website)
    if active_only:
        query = query.filter(Website.is_active.is_(True))
    return query.order_by(Website.name).all()


def search_websites(
    db: Session,
    *,
    query: str | None = None,
    city_id: int | None = None,
    onboarding_status: str | None = None,
    page: int = 1,
    per_page: int = 20,
) -> tuple[list[Website], int]:
    q = db.query(Website)
    if query:
        like = f"%{query}%"
        q = q.filter(or_(Website.name.ilike(like), Website.base_url.ilike(like)))
    if city_id is not None:
        q = q.filter(Website.city_id == city_id)
    if onboarding_status:
        q = q.filter(Website.onboarding_status == onboarding_status)

    total = q.count()
    page = max(page, 1)
    items = q.order_by(Website.name).offset((page - 1) * per_page).limit(per_page).all()
    return items, total


def count_websites(db: Session, *, active: bool | None = None) -> int:
    q = db.query(Website)
    if active is not None:
        q = q.filter(Website.is_active.is_(active))
    return q.count()
