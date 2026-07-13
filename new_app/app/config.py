from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    app_name: str = "New City Events App"
    app_env: str = "development"
    app_port: int = 8100
    database_url: str = f"sqlite:///{(BASE_DIR / 'app.db').as_posix()}"
    log_level: str = "INFO"

    # Auth / sessions. Local password login is a development/fallback mechanism —
    # flip `local_login_enabled` off once an external identity provider is wired up.
    local_login_enabled: bool = True
    session_cookie_name: str = "session_token"
    session_ttl_seconds: int = 43200  # 12 hours
    csrf_cookie_name: str = "csrf_token"
    # Secure flag requires HTTPS; keep False for local http:// dev, set True in production.
    cookie_secure: bool = False

    # Public self-registration. Enabled by default in development; disable via
    # env var once real deployment/anti-abuse controls are in place.
    registration_enabled: bool = True
    # Minimum password length for local accounts (registration and, in the
    # future, any local password change). Not a complex composition policy —
    # just a sane floor, per project convention (see scripts/create_superadmin.py).
    minimum_password_length: int = 8
    # Best-effort, dev-safe guard only — see app/services/rate_limit.py for
    # why this is not a production-grade rate limiter.
    registration_rate_limit_per_hour: int = 20

    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
