from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.city import City
from app.schemas.city import CityCreate, CityUpdate


def create_city(db: Session, data: CityCreate) -> City:
    city = City(**data.model_dump())
    db.add(city)
    db.commit()
    db.refresh(city)
    return city


def update_city(db: Session, city: City, data: CityUpdate) -> City:
    for field, value in data.model_dump().items():
        setattr(city, field, value)
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


def search_cities(
    db: Session,
    *,
    query: str | None = None,
    status: str = "all",
    page: int = 1,
    per_page: int = 20,
) -> tuple[list[City], int]:
    """Search/filter/paginate cities for the admin list page."""
    q = db.query(City)
    if query:
        like = f"%{query}%"
        q = q.filter(or_(City.name.ilike(like), City.slug.ilike(like)))
    if status == "active":
        q = q.filter(City.is_active.is_(True))
    elif status == "inactive":
        q = q.filter(City.is_active.is_(False))

    total = q.count()
    page = max(page, 1)
    items = q.order_by(City.name).offset((page - 1) * per_page).limit(per_page).all()
    return items, total


def count_cities(db: Session, *, active: bool | None = None) -> int:
    q = db.query(City)
    if active is not None:
        q = q.filter(City.is_active.is_(active))
    return q.count()
