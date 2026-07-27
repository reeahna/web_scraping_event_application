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

    # Production hardening (Phase 18). Defaults are development-safe; production
    # readiness is asserted by app.core.production_checks and surfaced at
    # /health/ready. `trusted_hosts` (when set) enables host-header validation;
    # `security_headers_enabled` adds CSP/clickjacking/nosniff/referrer headers
    # (and HSTS when behind HTTPS). `rate_limit_backend` selects the limiter
    # store — "memory" (dev) or an external store ("redis"/"database") in prod.
    trusted_hosts: list[str] = []
    security_headers_enabled: bool = True
    behind_https: bool = False
    content_security_policy: str = (
        "default-src 'self'; img-src 'self' data: https:; "
        "style-src 'self' 'unsafe-inline' https://unpkg.com; "
        "script-src 'self' https://unpkg.com; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    )
    max_request_bytes: int = 2_000_000
    rate_limit_backend: str = "memory"
    redis_url: str | None = None

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

    # Single application-wide timezone used to compute "today" for public
    # event visibility (see app/repositories/public_events.py). Deliberately
    # not per-city or per-viewer for this MVP — per-city date-boundary
    # handling is deferred to a later phase rather than mixing timezone
    # semantics inconsistently across events from different cities.
    app_timezone: str = "UTC"

    # Bulk source onboarding (app.services.bulk_onboarding). Caps exist so a
    # single submission can't queue an unbounded amount of outbound fetching,
    # and so a synchronous processing request stays responsive.
    onboarding_max_urls_per_batch: int = 100
    onboarding_max_csv_rows: int = 200
    onboarding_max_csv_bytes: int = 1_000_000
    onboarding_max_url_length: int = 2000
    # How many jobs one "process" request works through before returning. The
    # admin UI continues the batch with a further request; a Phase 10 worker
    # would instead loop until the batch drains.
    onboarding_jobs_per_request: int = 10

    # Optional AI configuration assistant (app.services.ai). Disabled by
    # default: the application is fully functional with no provider, and
    # recurring extraction never depends on it. `ai_provider` selects the
    # adapter ("disabled" | "echo"); real network adapters would be added
    # here but require an explicitly-configured key, which development and
    # tests never supply. Budgets are hard ceilings enforced before any call.
    ai_enabled: bool = False
    ai_provider: str = "disabled"
    ai_api_key: str | None = None
    ai_model: str = "unset"
    ai_timeout_seconds: float = 20.0
    ai_max_retries: int = 1
    ai_daily_request_limit: int = 200
    ai_monthly_request_limit: int = 3000
    # Circuit breaker: after this many consecutive failures the provider is
    # treated as unhealthy until the cooldown elapses.
    ai_failure_threshold: int = 5
    ai_cooldown_seconds: int = 300
    # Bound on how much evidence may be sent, so a large page can never be
    # forwarded wholesale to a third party.
    ai_max_sample_cards: int = 6
    ai_max_evidence_chars: int = 20000

    # Asynchronous geocoding (app.services.geocoding). Disabled by default: the
    # application is fully functional without it and no live third-party
    # request is ever made unless an administrator turns it on. `geocoding_provider`
    # selects the adapter ("disabled" | "nominatim"). Nominatim needs no API
    # key but requires a descriptive user agent and strict rate limiting per its
    # usage policy — both enforced here rather than trusted to the caller.
    geocoding_enabled: bool = False
    geocoding_provider: str = "disabled"
    geocoding_user_agent: str = "city-events-app/1.0 (contact: admin@example.com)"
    geocoding_timeout_seconds: float = 10.0
    geocoding_max_retries: int = 2
    # Minimum seconds between outbound requests to the provider (Nominatim's
    # public policy is at most one request per second).
    geocoding_min_interval_seconds: float = 1.0
    geocoding_failure_threshold: int = 5
    geocoding_cooldown_seconds: int = 300
    # How many events one background drain processes per tick.
    geocoding_batch_size: int = 10

    # Public UI (Phase 12). A configurable fallback image shown on event cards
    # and detail pages that have no image; None uses the built-in inline icon.
    public_fallback_image_url: str | None = None
    # OpenStreetMap raster tile template for the Leaflet map. Overridable so an
    # operator can point at their own tile server / attribution.
    public_map_tile_url: str = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
    public_map_attribution: str = "© OpenStreetMap contributors"

    # Email delivery for alerts (Phase 13). Off by default: no email is ever
    # sent unless explicitly enabled AND a real backend is configured. The
    # built-in backends are "noop" (records nothing) and "console" (logs). A
    # real SMTP/API backend would be added here behind explicit credentials.
    email_enabled: bool = False
    email_backend: str = "noop"
    email_from: str = "alerts@example.com"

    # External authentication (Phase 14). Each provider is independently
    # configured and is considered ENABLED only when it has a client id AND
    # secret — so the app starts fine with any or all disabled, and no provider
    # is ever offered without credentials. No real OAuth app is required for
    # development or tests (those use a mocked provider).
    oauth_redirect_base_url: str = "http://localhost:8000"
    google_client_id: str | None = None
    google_client_secret: str | None = None
    microsoft_client_id: str | None = None
    microsoft_client_secret: str | None = None
    facebook_client_id: str | None = None
    facebook_client_secret: str | None = None

    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
