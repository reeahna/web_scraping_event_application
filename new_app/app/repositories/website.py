from sqlalchemy.orm import Session

from app.models.website import Website
from app.schemas.website import WebsiteCreate


def create_website(db: Session, data: WebsiteCreate) -> Website:
    website = Website(**data.model_dump())
    db.add(website)
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
