import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine, inspect, text

BASE_DIR = Path(__file__).resolve().parent.parent
MIGRATION_TMP_DIR = BASE_DIR / "tests" / ".tmp"
MIGRATION_TMP_DIR.mkdir(exist_ok=True)

EXPECTED_TABLES = {
    "users",
    "roles",
    "permissions",
    "user_roles",
    "role_permissions",
    "cities",
    "websites",
    "event_categories",
    "categorization_rules",
    "events",
    "audit_logs",
    "alembic_version",
    "user_sessions",
    "extraction_runs",
    "extraction_errors",
    "event_provenance",
    "unsupported_site_reports",
}


def _run_alembic(database_url: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "DATABASE_URL": database_url}
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(BASE_DIR),
        env=env,
        capture_output=True,
        text=True,
    )


def _database_url(filename: str) -> tuple[Path, str]:
    db_file = MIGRATION_TMP_DIR / filename
    db_file.unlink(missing_ok=True)
    return db_file, f"sqlite:///{db_file.as_posix()}"


def test_alembic_upgrade_head_creates_all_tables():
    db_file, database_url = _database_url("migration_test.db")
    result = _run_alembic(database_url, "upgrade", "head")
    assert result.returncode == 0, result.stderr

    engine = create_engine(database_url)
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    assert EXPECTED_TABLES.issubset(tables)
    db_file.unlink(missing_ok=True)


def test_viewer_rename_upgrade_downgrade_and_reupgrade_preserve_assignment():
    db_file, database_url = _database_url("role_rename_round_trip.db")
    before_head = _run_alembic(database_url, "upgrade", "ac034c0f9ec1")
    assert before_head.returncode == 0, before_head.stderr

    engine = create_engine(database_url)
    now = datetime.now(UTC)
    with engine.begin() as connection:
        # Older migrations import today's seed catalog, so reshape that seeded
        # row to the historical pre-migration state this revision receives.
        connection.execute(
            text(
                "UPDATE roles SET name = 'Viewer', description = :description "
                "WHERE name = 'Registered User'"
            ),
            {"description": "Default 'Viewer' role"},
        )
        viewer_id = connection.execute(
            text("SELECT id FROM roles WHERE name = 'Viewer'")
        ).scalar_one()
        connection.execute(
            text(
                "INSERT INTO users "
                "(email, full_name, hashed_password, is_active, created_at, "
                "updated_at, last_login_at) "
                "VALUES (:email, NULL, NULL, 1, :now, :now, NULL)"
            ),
            {"email": "migration-user@example.com", "now": now},
        )
        user_id = connection.execute(
            text("SELECT id FROM users WHERE email = 'migration-user@example.com'")
        ).scalar_one()
        connection.execute(
            text(
                "INSERT INTO user_roles (user_id, role_id, created_at, updated_at) "
                "VALUES (:uid, :rid, :now, :now)"
            ),
            {"uid": user_id, "rid": viewer_id, "now": now},
        )

    upgrade = _run_alembic(database_url, "upgrade", "head")
    assert upgrade.returncode == 0, upgrade.stderr
    with engine.connect() as connection:
        role_rows = connection.execute(
            text("SELECT id, name FROM roles WHERE name IN ('Viewer', 'Registered User')")
        ).all()
        assert role_rows == [(viewer_id, "Registered User")]
        assert (
            connection.execute(
                text("SELECT role_id FROM user_roles WHERE user_id = :uid"), {"uid": user_id}
            ).scalar_one()
            == viewer_id
        )
        assert (
            connection.execute(
                text("SELECT COUNT(*) FROM role_permissions WHERE role_id = :rid"),
                {"rid": viewer_id},
            ).scalar_one()
            == 0
        )

    downgrade = _run_alembic(database_url, "downgrade", "ac034c0f9ec1")
    assert downgrade.returncode == 0, downgrade.stderr
    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT name FROM roles WHERE id = :rid"), {"rid": viewer_id}
            ).scalar_one()
            == "Viewer"
        )
        assert (
            connection.execute(
                text("SELECT role_id FROM user_roles WHERE user_id = :uid"), {"uid": user_id}
            ).scalar_one()
            == viewer_id
        )

    reupgrade = _run_alembic(database_url, "upgrade", "head")
    assert reupgrade.returncode == 0, reupgrade.stderr
    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT name FROM roles WHERE id = :rid"), {"rid": viewer_id}
            ).scalar_one()
            == "Registered User"
        )
    engine.dispose()
    db_file.unlink(missing_ok=True)


def test_role_rename_merges_unexpected_duplicate_without_duplicate_assignments():
    db_file, database_url = _database_url("role_rename_conflict.db")
    before_head = _run_alembic(database_url, "upgrade", "ac034c0f9ec1")
    assert before_head.returncode == 0, before_head.stderr

    engine = create_engine(database_url)
    now = datetime.now(UTC)
    with engine.begin() as connection:
        registered_id = connection.execute(
            text("SELECT id FROM roles WHERE name = 'Registered User'")
        ).scalar_one()
        connection.execute(
            text(
                "INSERT INTO roles (name, description, is_active, created_at, updated_at) "
                "VALUES ('Viewer', 'Historical viewer', 1, :now, :now)"
            ),
            {"now": now},
        )
        viewer_id = connection.execute(
            text("SELECT id FROM roles WHERE name = 'Viewer'")
        ).scalar_one()
        connection.execute(
            text(
                "INSERT INTO users "
                "(email, full_name, hashed_password, is_active, created_at, "
                "updated_at, last_login_at) "
                "VALUES ('merge-user@example.com', NULL, NULL, 1, :now, :now, NULL)"
            ),
            {"now": now},
        )
        user_id = connection.execute(
            text("SELECT id FROM users WHERE email = 'merge-user@example.com'")
        ).scalar_one()
        for role_id in (registered_id, viewer_id):
            connection.execute(
                text(
                    "INSERT INTO user_roles (user_id, role_id, created_at, updated_at) "
                    "VALUES (:uid, :rid, :now, :now)"
                ),
                {"uid": user_id, "rid": role_id, "now": now},
            )

    upgrade = _run_alembic(database_url, "upgrade", "head")
    assert upgrade.returncode == 0, upgrade.stderr
    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT COUNT(*) FROM roles WHERE name = 'Registered User'")
            ).scalar_one()
            == 1
        )
        assert (
            connection.execute(
                text("SELECT COUNT(*) FROM roles WHERE name = 'Viewer'")
            ).scalar_one()
            == 0
        )
        assignments = (
            connection.execute(
                text("SELECT role_id FROM user_roles WHERE user_id = :uid"), {"uid": user_id}
            )
            .scalars()
            .all()
        )
        assert assignments == [registered_id]
    engine.dispose()
    db_file.unlink(missing_ok=True)


def test_phase5_migration_round_trip_preserves_existing_events():
    db_file, database_url = _database_url("phase5_round_trip.db")
    before_phase5 = _run_alembic(database_url, "upgrade", "c8abf388f25c")
    assert before_phase5.returncode == 0, before_phase5.stderr

    engine = create_engine(database_url)
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO events "
                "(title, canonical_url, source, is_active, archived_at, created_at, updated_at) "
                "VALUES ('Preserved Event', 'https://example.com/preserved', 'Historical', "
                "1, NULL, :now, :now)"
            ),
            {"now": now},
        )
        event_id = connection.execute(
            text("SELECT id FROM events WHERE title = 'Preserved Event'")
        ).scalar_one()

    upgrade = _run_alembic(database_url, "upgrade", "head")
    assert upgrade.returncode == 0, upgrade.stderr
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT title, origin, review_status, duplicate_status FROM events WHERE id = :id"
            ),
            {"id": event_id},
        ).one()
        assert row == ("Preserved Event", "extracted", "needs_review", "not_reviewed")
        assert connection.execute(text("SELECT COUNT(*) FROM event_categories")).scalar_one() >= 14

    downgrade = _run_alembic(database_url, "downgrade", "c8abf388f25c")
    assert downgrade.returncode == 0, downgrade.stderr
    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT title FROM events WHERE id = :id"), {"id": event_id}
            ).scalar_one()
            == "Preserved Event"
        )

    reupgrade = _run_alembic(database_url, "upgrade", "head")
    assert reupgrade.returncode == 0, reupgrade.stderr
    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT title FROM events WHERE id = :id"), {"id": event_id}
            ).scalar_one()
            == "Preserved Event"
        )
    engine.dispose()
    db_file.unlink(missing_ok=True)


def test_phase6_migration_round_trip_preserves_websites_and_events():
    db_file, database_url = _database_url("phase6_round_trip.db")
    before_phase6 = _run_alembic(database_url, "upgrade", "f5c3d9a1e7b2")
    assert before_phase6.returncode == 0, before_phase6.stderr

    engine = create_engine(database_url)
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO cities (name, slug, timezone, is_active, created_at, updated_at) "
                "VALUES ('Preserved City', 'preserved-city', 'UTC', 1, :now, :now)"
            ),
            {"now": now},
        )
        city_id = connection.execute(
            text("SELECT id FROM cities WHERE slug = 'preserved-city'")
        ).scalar_one()
        connection.execute(
            text(
                "INSERT INTO websites "
                "(name, city_id, base_url, requires_js, is_active, onboarding_status, "
                "consecutive_failure_count, created_at, updated_at) "
                "VALUES ('Preserved Site', :city_id, 'https://example.com', 0, 0, 'draft', "
                "0, :now, :now)"
            ),
            {"city_id": city_id, "now": now},
        )
        website_id = connection.execute(
            text("SELECT id FROM websites WHERE name = 'Preserved Site'")
        ).scalar_one()
        connection.execute(
            text(
                "INSERT INTO events "
                "(title, canonical_url, source, website_id, city_id, is_active, "
                "review_status, duplicate_status, category_source, origin, "
                "created_at, updated_at) "
                "VALUES ('Preserved Event', 'https://example.com/preserved', 'Historical', "
                ":website_id, :city_id, 1, 'needs_review', 'not_reviewed', 'uncategorized', "
                "'extracted', :now, :now)"
            ),
            {"website_id": website_id, "city_id": city_id, "now": now},
        )
        event_id = connection.execute(
            text("SELECT id FROM events WHERE title = 'Preserved Event'")
        ).scalar_one()

    upgrade = _run_alembic(database_url, "upgrade", "head")
    assert upgrade.returncode == 0, upgrade.stderr
    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT title FROM events WHERE id = :id"), {"id": event_id}
            ).scalar_one()
            == "Preserved Event"
        )
        website_row = connection.execute(
            text(
                "SELECT name, onboarding_status, configuration_version, "
                "active_configuration_version, approved_at "
                "FROM websites WHERE id = :id"
            ),
            {"id": website_id},
        ).one()
        assert website_row == ("Preserved Site", "draft", 0, None, None)
        assert set(inspect(engine).get_table_names()).issuperset(
            {"extraction_runs", "extraction_errors", "event_provenance", "unsupported_site_reports"}
        )

    downgrade = _run_alembic(database_url, "downgrade", "f5c3d9a1e7b2")
    assert downgrade.returncode == 0, downgrade.stderr
    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT name FROM websites WHERE id = :id"), {"id": website_id}
            ).scalar_one()
            == "Preserved Site"
        )
        assert (
            connection.execute(
                text("SELECT title FROM events WHERE id = :id"), {"id": event_id}
            ).scalar_one()
            == "Preserved Event"
        )
        remaining_tables = set(inspect(engine).get_table_names())
        assert not remaining_tables.intersection(
            {"extraction_runs", "extraction_errors", "event_provenance", "unsupported_site_reports"}
        )

    reupgrade = _run_alembic(database_url, "upgrade", "head")
    assert reupgrade.returncode == 0, reupgrade.stderr
    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT title FROM events WHERE id = :id"), {"id": event_id}
            ).scalar_one()
            == "Preserved Event"
        )
    engine.dispose()
    db_file.unlink(missing_ok=True)
