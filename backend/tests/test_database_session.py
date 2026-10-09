from sqlalchemy import text

from app.database import get_db


def test_get_db_yields_working_session():
    gen = get_db()
    session = next(gen)
    try:
        result = session.execute(text("SELECT 1")).scalar()
        assert result == 1
    finally:
        gen.close()
