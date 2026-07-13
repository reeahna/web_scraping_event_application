from sqlalchemy.orm import Session

from app.models.city import City
from app.schemas.city import CityCreate


def create_city(db: Session, data: CityCreate) -> City:
    city = City(**data.model_dump())
    db.add(city)
    db.commit()
    db.refresh(city)
    return city


def get_city(db: Session, city_id: int) -> City | None:
    return db.get(City, city_id)


def get_city_by_slug(db: Session, slug: str) -> City | None:
    return db.query(City).filter(City.slug == slug).first()


def list_cities(db: Session, *, active_only: bool = True) -> list[City]:
    query = db.query(City)
    if active_only:
        query = query.filter(City.is_active.is_(True))
    return query.order_by(City.name).all()
