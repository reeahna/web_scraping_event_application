from app.models.city import City
from app.models.role import Role
from app.models.user import User
from app.models.user_role import UserRole
from app.repositories.city import create_city
from app.repositories.website import create_website
from app.schemas.city import CityCreate
from app.schemas.website import WebsiteCreate


def test_deleting_city_sets_website_city_id_null(db_session):
    city = create_city(db_session, CityCreate(name="Test City", slug="test-city"))
    website = create_website(
        db_session,
        WebsiteCreate(name="Test Site", base_url="https://example.com", city_id=city.id),
    )

    db_session.delete(db_session.get(City, city.id))
    db_session.commit()

    db_session.refresh(website)
    assert website.city_id is None


def test_deleting_user_cascades_to_user_roles(db_session):
    user = User(email="test@example.com")
    role = Role(name="admin")
    db_session.add_all([user, role])
    db_session.commit()

    link = UserRole(user_id=user.id, role_id=role.id)
    db_session.add(link)
    db_session.commit()
    link_id = link.id

    db_session.delete(user)
    db_session.commit()

    assert db_session.get(UserRole, link_id) is None
