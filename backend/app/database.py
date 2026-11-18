from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

settings = get_settings()
_is_sqlite = settings.database_url.startswith("sqlite")

# How long a SQLite connection waits for another one's write to finish before
# giving up with "database is locked" (seconds).
SQLITE_BUSY_TIMEOUT_SECONDS = 30

connect_args = (
    {"check_same_thread": False, "timeout": SQLITE_BUSY_TIMEOUT_SECONDS}
    if _is_sqlite
    else {}
)
engine = create_engine(settings.database_url, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

if _is_sqlite:
    @event.listens_for(engine, "connect")
    def _configure_sqlite_connection(dbapi_connection, connection_record) -> None:
        cursor = dbapi_connection.cursor()
        # SQLite ignores FK constraints unless explicitly enabled per-connection.
        cursor.execute("PRAGMA foreign_keys=ON")
        # Write-ahead logging lets pages keep reading while the scheduler is
        # writing (scrapes, imports, sweeps). In the default journal mode a long
        # write blocks every reader, and pages failed with "database is locked".
        # The setting is stored in the database file, so this is a no-op after
        # the first connection. An in-memory database answers "memory" instead.
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_SECONDS * 1000}")
        cursor.close()


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
