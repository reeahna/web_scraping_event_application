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
from app.database import Base, SessionLocal, engine  # noqa: E402
from app.main import app as fastapi_app  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_db():
    Base.metadata.create_all(bind=engine)
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
