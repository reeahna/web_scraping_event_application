import os
from pathlib import Path

# Point the app at a dedicated test database *before* importing anything from
# `app` — app.database builds its engine at import time from this env var, so
# order matters. Keeps tests fully isolated from both the dev app.db and the
# legacy_app database.
TESTS_DIR = Path(__file__).resolve().parent
TEST_DB_PATH = TESTS_DIR / "test_app.db"
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB_PATH.as_posix()}"

import pytest  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

import app.models  # noqa: E402, F401  (registers all models on Base.metadata)
from app.config import get_settings  # noqa: E402
from app.core.permissions import SUPER_ADMINISTRATOR  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.core.seed import seed_defaults  # noqa: E402
from app.database import Base, SessionLocal, engine  # noqa: E402
from app.main import app as fastapi_app  # noqa: E402
from app.models.role import Role  # noqa: E402
from app.models.user import User  # noqa: E402
from app.models.user_role import UserRole  # noqa: E402

settings = get_settings()


@pytest.fixture(autouse=True)
def _reset_db():
    Base.metadata.create_all(bind=engine)
    seed_session = SessionLocal()
    try:
        seed_defaults(seed_session)
    finally:
        seed_session.close()
    yield
    Base.metadata.drop_all(bind=engine)


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
            email=email,
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
