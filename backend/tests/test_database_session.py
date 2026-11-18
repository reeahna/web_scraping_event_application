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


def test_sqlite_connections_use_wal_and_wait_for_writers():
    from app.database import SQLITE_BUSY_TIMEOUT_SECONDS, engine

    if engine.dialect.name != "sqlite":
        return
    with engine.connect() as connection:
        mode = connection.exec_driver_sql("PRAGMA journal_mode").scalar()
        timeout = connection.exec_driver_sql("PRAGMA busy_timeout").scalar()
    assert mode == "wal"
    assert timeout == SQLITE_BUSY_TIMEOUT_SECONDS * 1000


def test_sqlite_reader_is_not_blocked_by_open_write(tmp_path):
    """With WAL, a page can read while the scheduler holds a write open."""
    import sqlite3

    path = tmp_path / "wal.db"
    writer = sqlite3.connect(path, timeout=0.1, isolation_level=None)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE t (x INTEGER)")
    writer.execute("INSERT INTO t VALUES (1)")
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO t VALUES (2)")
    reader = sqlite3.connect(path, timeout=0.1)
    try:
        assert reader.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1
    finally:
        reader.close()
        writer.execute("COMMIT")
        writer.close()
