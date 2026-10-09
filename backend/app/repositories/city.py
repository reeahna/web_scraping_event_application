from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.city import City
from app.models.city_university import CityUniversity
from app.schemas.city import CityCreate, CityUpdate


def _set_universities(city: City, names: list[str]) -> None:
    """Replace a town's schools with `names`, keeping rows that already match.

    Reusing the existing row for an unchanged name keeps its id stable, so a
    plain edit elsewhere on the form does not churn these rows.
    """
    wanted = []
    for name in names:
        cleaned = name.strip()
        if cleaned and cleaned not in wanted:
            wanted.append(cleaned)

    existing = {u.name: u for u in city.universities}
    city.universities = [
        existing.get(name) or CityUniversity(name=name) for name in wanted
    ]


def create_city(db: Session, data: CityCreate) -> City:
    values = data.model_dump()
    universities = values.pop("universities", [])
    city = City(**values)
    _set_universities(city, universities)
    db.add(city)
    db.commit()
    db.refresh(city)
    return city


def update_city(db: Session, city: City, data: CityUpdate) -> City:
    values = data.model_dump()
    universities = values.pop("universities", [])
    for field, value in values.items():
        setattr(city, field, value)
    _set_universities(city, universities)
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


def search_public_cities(db: Session, *, query: str | None = None) -> list[City]:
    """Active cities matching a free-text query, for the public city chooser.

    Matches the town, the school and the state, because a visitor is as likely
    to think "Lehigh" or "Indiana" as "Bethlehem" or "Bloomington".
    """
    statement = db.query(City).filter(City.is_active.is_(True))
    term = (query or "").strip()
    if term:
        like = f"%{term}%"
        # A correlated EXISTS rather than a join, so a town with two schools
        # matching the term still comes back once.
        matches_school = (
            select(CityUniversity.id)
            .where(CityUniversity.city_id == City.id, CityUniversity.name.ilike(like))
            .exists()
        )
        statement = statement.filter(
            or_(
                City.name.ilike(like),
                City.state_or_region.ilike(like),
                matches_school,
            )
        )
    return statement.order_by(City.name).all()


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
