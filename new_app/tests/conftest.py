import os
from pathlib import Path

# Point the app at a dedicated test database *before* importing anything from
# `app` — app.database builds its engine at import time from this env var, so
# order matters. Keeps tests fully isolated from both the dev app.db and the
# legacy_app database.
TESTS_DIR = Path(__file__).resolve().parent
TEST_DB_PATH = TESTS_DIR / "test_app.db"
TEST_TMP_PATH = TESTS_DIR / ".tmp"
TEST_TMP_PATH.mkdir(exist_ok=True)
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB_PATH.as_posix()}"
os.environ["TEMP"] = str(TEST_TMP_PATH)
os.environ["TMP"] = str(TEST_TMP_PATH)

import pytest  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

import app.models  # noqa: E402, F401  (registers all models on Base.metadata)
from app.config import get_settings  # noqa: E402
from app.core.email import normalize_email  # noqa: E402
from app.core.permissions import SUPER_ADMINISTRATOR  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.core.seed import seed_defaults  # noqa: E402
from app.database import Base, SessionLocal, engine  # noqa: E402
from app.main import app as fastapi_app  # noqa: E402
from app.models.city import City  # noqa: E402
from app.models.event import Event  # noqa: E402
from app.models.event_category import EventCategory  # noqa: E402
from app.models.role import Role  # noqa: E402
from app.models.user import User  # noqa: E402
from app.models.user_role import UserRole  # noqa: E402
from app.models.website import Website  # noqa: E402
from app.services.rate_limit import _attempts_by_ip  # noqa: E402

settings = get_settings()


@pytest.fixture(autouse=True)
def _reset_db():
    _attempts_by_ip.clear()
    Base.metadata.create_all(bind=engine)
    seed_session = SessionLocal()
    try:
        seed_defaults(seed_session)
    finally:
        seed_session.close()
    yield
    Base.metadata.drop_all(bind=engine)
    _attempts_by_ip.clear()


@pytest.fixture
def db_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client():
    with TestClient(fastapi_app) as c:
        yield c


def _assign_role(db_session, user: User, role_name: str) -> None:
    role = db_session.query(Role).filter(Role.name == role_name).one()
    db_session.add(UserRole(user_id=user.id, role_id=role.id))
    db_session.commit()


@pytest.fixture
def make_user(db_session):
    """Factory fixture: make_user(email, password, role_name=None, is_active=True)."""

    def _make_user(
        email: str = "user@example.com",
        password: str = "correct-horse-battery",
        role_name: str | None = None,
        is_active: bool = True,
    ) -> User:
        user = User(
            email=normalize_email(email),
            hashed_password=hash_password(password),
            is_active=is_active,
        )
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)
        if role_name:
            _assign_role(db_session, user, role_name)
        return user

    return _make_user


@pytest.fixture
def make_super_admin(make_user):
    def _make(email: str = "root@example.com", password: str = "correct-horse-battery") -> User:
        return make_user(email=email, password=password, role_name=SUPER_ADMINISTRATOR)

    return _make


@pytest.fixture
def make_city(db_session):
    def _make_city(
        name: str = "Test City",
        slug: str = "test-city",
        timezone: str = "UTC",
        is_active: bool = True,
    ) -> City:
        city = City(name=name, slug=slug, timezone=timezone, is_active=is_active)
        db_session.add(city)
        db_session.commit()
        db_session.refresh(city)
        return city

    return _make_city


@pytest.fixture
def make_event(db_session):
    def _make_event(
        city: City,
        title: str = "Test Event",
        canonical_url: str = "https://example.com/event",
        archived: bool = False,
        website: Website | None = None,
        category: EventCategory | None = None,
        is_active: bool = True,
        review_status: str = "needs_review",
        duplicate_status: str = "not_reviewed",
        **values,
    ) -> Event:
        from datetime import UTC, datetime

        event = Event(
            title=title,
            canonical_url=canonical_url,
            source="Test Source",
            city_id=city.id,
            website_id=website.id if website else None,
            category_id=category.id if category else None,
            is_active=is_active,
            review_status=review_status,
            duplicate_status=duplicate_status,
            archived_at=datetime.now(UTC) if archived else None,
            **values,
        )
        db_session.add(event)
        db_session.commit()
        db_session.refresh(event)
        return event

    return _make_event


@pytest.fixture
def make_category(db_session):
    def _make_category(
        name: str = "Test Category",
        slug: str = "test-category",
        is_active: bool = True,
    ) -> EventCategory:
        category = EventCategory(name=name, slug=slug, is_active=is_active)
        db_session.add(category)
        db_session.commit()
        db_session.refresh(category)
        return category

    return _make_category


@pytest.fixture
def make_website(db_session):
    def _make_website(
        city: City,
        name: str = "Test Site",
        base_url: str = "https://example.com",
        archived: bool = False,
    ) -> Website:
        from datetime import UTC, datetime

        website = Website(
            name=name,
            base_url=base_url,
            city_id=city.id,
            archived_at=datetime.now(UTC) if archived else None,
        )
        db_session.add(website)
        db_session.commit()
        db_session.refresh(website)
        return website

    return _make_website


@pytest.fixture
def login(client):
    """Factory fixture: login(email, password) -> Response from POST /auth/login.
    Leaves the TestClient's cookie jar populated with the session cookie on
    success, exactly like a real browser."""

    def _login(email: str, password: str):
        client.get("/auth/login")
        csrf = client.cookies.get(settings.csrf_cookie_name)
        return client.post(
            "/auth/login",
            data={"email": email, "password": password, "csrf_token": csrf},
            follow_redirects=False,
        )

    return _login


@pytest.fixture
def register(client):
    """Submit the public registration form with a valid CSRF token."""

    def _register(**overrides):
        client.get("/register")
        data = {
            "display_name": "New User",
            "email": "new-user@example.com",
            "password": "registration-pass-123",
            "password_confirm": "registration-pass-123",
            "csrf_token": client.cookies.get(settings.csrf_cookie_name),
        }
        data.update(overrides)
        return client.post("/register", data=data, follow_redirects=False)

    return _register
