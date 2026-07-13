import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, inspect

BASE_DIR = Path(__file__).resolve().parent.parent

EXPECTED_TABLES = {
    "users",
    "roles",
    "permissions",
    "user_roles",
    "role_permissions",
    "cities",
    "websites",
    "event_categories",
    "events",
    "audit_logs",
    "alembic_version",
}


def test_alembic_upgrade_head_creates_all_tables(tmp_path):
    db_file = tmp_path / "migration_test.db"
    database_url = f"sqlite:///{db_file.as_posix()}"
    env = {**os.environ, "DATABASE_URL": database_url}

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(BASE_DIR),
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    engine = create_engine(database_url)
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    assert EXPECTED_TABLES.issubset(tables)
