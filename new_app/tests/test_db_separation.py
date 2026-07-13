from pathlib import Path

LEGACY_DB = Path(__file__).resolve().parents[2] / "legacy_app" / "events.db"


def test_default_database_url_field_never_points_at_legacy_db():
    from app.config import Settings

    default = Settings.model_fields["database_url"].default
    assert "legacy_app" not in default
    assert "new_app" in default.replace("\\", "/") or default.endswith("app.db")


def test_active_test_database_differs_from_legacy_db():
    from app.config import get_settings

    settings = get_settings()
    resolved = Path(settings.database_url.removeprefix("sqlite:///")).resolve()
    assert resolved != LEGACY_DB.resolve()
    assert resolved.name != "events.db"
