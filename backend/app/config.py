import os
from functools import lru_cache
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


def _is_local_url(value: str | None) -> bool:
    """True for the development default and anything else pointing at this box."""
    lowered = (value or "").lower()
    return not lowered or "localhost" in lowered or "127.0.0.1" in lowered


class Settings(BaseSettings):
    # The brand, used for the page titles, the header, the footer and the
    # Open Graph site name. Templates read it as `site_name`, so a rename is
    # this line rather than a sweep through every template.
    app_name: str = "Bulletin"
    app_env: str = "development"
    app_port: int = 8100
    # The site's own address, used for canonical links, Open Graph URLs and the
    # sitemap. Those have to be absolute, and a request's Host header is
    # attacker-controlled, so it is configuration rather than something derived
    # per request.
    public_base_url: str = "http://localhost:8100"
    # A short public description, used as the default meta description and as
    # the Open Graph site name's companion text.
    public_tagline: str = "Local events, gathered in one place."
    # Where people reach the site's operator, shown on the privacy policy and
    # terms (account questions, data requests, a source asking to be removed).
    contact_email: str | None = None
    database_url: str = f"sqlite:///{(BASE_DIR / 'app.db').as_posix()}"
    log_level: str = "INFO"

    # Auth / sessions. Sign-in is through Google, Microsoft, Facebook, X or Yahoo
    # (app.services.oauth) or a code sent by email (app.services.email_sign_in);
    # the app stores no passwords.
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

    # Whether a first sign-in creates a new account. Off = only people who
    # already have an account (and SUPERADMIN_EMAIL) can sign in.
    registration_enabled: bool = True
    # The address that is always made a Super Administrator when it signs in,
    # so the site can never be left without one. Only honoured when the sign-in
    # provider has verified the address (in practice: Google).
    superadmin_email: str | None = None
    # Sign-in attempts started per IP address in 15 minutes. Each start stores a
    # one-time state record, so this keeps an anonymous visitor from filling
    # that table.
    sign_in_starts_per_15_minutes: int = 30
    # A "Sign in as the test user" button on the sign-in page, for trying the
    # site as an ordinary signed-in person in development or staging without a
    # Google/Microsoft/Facebook account. The account (TEST_USER_EMAIL, a
    # Registered User) is created on its first use. Anyone who can reach the
    # page can use the button, so the app refuses to start with it on when
    # APP_ENV=production.
    test_login_enabled: bool = False
    test_user_email: str = "test-user@bulletin.invalid"

    # Fallback timezone for "today" where there is no town to go by (the
    # visitor log, a town with no zone). Public event visibility uses each
    # town's own timezone (see app/repositories/public_events.py).
    app_timezone: str = "UTC"

    # Visitor log (app.services.site_visits, admin page /admin/visitors).
    # Counts public page views without cookies or stored IP addresses. Counts
    # are written in a batch every `visit_flush_seconds` and deleted after
    # `visit_retention_days`.
    visit_tracking_enabled: bool = True
    visit_flush_seconds: int = 60
    visit_retention_days: int = 180

    # Restricted browser-assisted recovery (app.services.browser_recovery).
    # Off by default: the restricted Playwright fetch strategy exists but is
    # never invoked unless an administrator turns this on for the deployment.
    # When off, the "Retry with restricted browser detection" action is hidden
    # and the recovery service refuses to launch a browser — so installing the
    # feature changes no source's behaviour until it is explicitly enabled.
    browser_extraction_enabled: bool = False
    # How many headless Chromium instances may run at once on the machine,
    # across the web server and the scheduler together. Each takes a few hundred
    # MB; on a 512MB instance shared with both, more than one at a time is what
    # runs it out of memory.
    browser_max_concurrent: int = 1

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

    # Gemini event categorization (app.services.ai_categorizer). A separate,
    # opt-in labeling pass that assigns each event a category with far better
    # accuracy than the keyword rules — it understands that a "cooking class" is
    # food, not education. Off unless a key is present. Runs once per event and
    # writes category_source="ai", so a re-scrape never re-calls the API and the
    # keyword rules never overwrite an AI label. Only the Google AI Studio API
    # key is needed (free tier). Batching + a minimum inter-request interval keep
    # a full-catalog pass inside the free rate limits.
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-3.6-flash"
    # Optional second model, tried once when gemini_model answers 503 "overloaded".
    # A lighter model is usually less busy and has its own free-tier quota.
    gemini_fallback_model: str | None = None
    # Events per request. The free tier caps requests (20 a day, 5 a minute for
    # Flash at the time of writing), not events, so a big batch stretches the
    # daily quota furthest; 100 events is still far inside the token limit.
    gemini_batch_size: int = 100
    # When a key is set, the scheduler process also labels newly imported events
    # in the background (one batch per dispatch tick), so events added by the
    # daily scrape get an AI category without a manual run. Turn off to keep the
    # key for the manual script only.
    gemini_scheduled_enabled: bool = True
    # Hide events the model says a college student would not go to (finance or
    # real-estate seminars, trade shows, professional conferences). Hiding only
    # clears is_active, so an administrator can bring an event back.
    gemini_hide_unwanted: bool = True
    # 13s keeps the manual script under the free tier's 5 requests a minute.
    gemini_min_interval_seconds: float = 13.0
    gemini_timeout_seconds: float = 60.0
    gemini_max_retries: int = 4

    # Groq (app.services.ai_categorizer.GroqCategorizer). When GROQ_API_KEY is
    # set it is used instead of Gemini, by the scheduler and the script alike;
    # the gemini_* settings above that are not model-specific (scheduled
    # labeling, hiding unwanted events) still apply. Groq retired Llama 3.3 70B
    # in August 2026; gpt-oss-120b is its suggested replacement. The free tier
    # allows about 1,000 requests a day but only ~8K tokens a minute, so
    # batches are small (25 events is ~6K tokens) and the script waits 60s
    # between them.
    groq_api_key: str | None = None
    groq_model: str = "openai/gpt-oss-120b"
    groq_batch_size: int = 25
    groq_min_interval_seconds: float = 60.0
    # Token savers, measured by the model evaluation on the AI labeling page
    # before being changed in production. ai_description_chars caps how much
    # of each event's description is sent (the biggest part of a request).
    # groq_prompt_layout "split" puts the fixed instructions in the system
    # message so every request starts with the same text, which Groq can
    # serve from its prompt cache (cached tokens do not count toward its
    # rate limits); "single" is the original one-message layout.
    ai_description_chars: int = 600
    groq_prompt_layout: str = "single"
    # When above 0, a batch is cut down until its prompt is estimated below
    # this many tokens, so a larger groq_batch_size can never exceed the
    # per-minute token limit (8K for gpt-oss on the Free plan) once the answer
    # is added. 0 (the default) leaves batches as they are; 6500 suits 8K.
    groq_max_prompt_tokens: int = 0
    # The cheaper model an administrator can compare with groq_model from the
    # AI labeling page (it has its own free-tier quota on Groq).
    groq_eval_candidate_model: str = "openai/gpt-oss-20b"
    # A second Groq model (its own free quota) to label with only while
    # groq_model is rate limited. Off unless set. Its labels are provisional:
    # it never hides an event, never replaces a groq_model label or an admin
    # override, and every event it labels stays queued for groq_model to
    # verify. Not enabled until the evaluation results are reviewed.
    groq_fallback_model: str | None = None

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
    # How many events one background drain processes per tick (one tick a
    # minute). At the one-request-per-second limit, 30 fits well inside a tick.
    geocoding_batch_size: int = 30

    # Leave out of the import anything that is not an event a person can go to:
    # online-only sessions, conferences, certification courses, internal staff
    # or faculty business, and deadline/reminder entries (see
    # app/services/attendability.py). On by default.
    attendability_filter_enabled: bool = True
    # Leave out events whose own address names a town other than the source's
    # city (an Eventbrite Bloomington page lists Indianapolis events too). A
    # source with its own geographic filter is left to it. See
    # app/services/home_town.py. On by default.
    home_town_filter_enabled: bool = True
    # Create the towns listed in data/college_towns/cities.csv and queue each
    # source CSV in that folder for onboarding, once per file (see
    # app/services/college_towns.py). Off by default.
    college_town_import_enabled: bool = False

    # Public UI (Phase 12). A configurable fallback image shown on event cards
    # and detail pages that have no image; None uses the built-in inline icon.
    public_fallback_image_url: str | None = None
    # Search engine ownership checks. The content value of the meta tag Google
    # Search Console ("HTML tag" method) or Bing Webmaster Tools gives, which
    # the page head then carries. These are public tokens, not secrets: they
    # only prove who controls the site. Unset adds no tag.
    google_site_verification: str | None = None
    bing_site_verification: str | None = None
    # Unsplash Access Key (Client-ID) for the category-photo placeholder pool.
    # Only the Access Key is needed (read-only search); the Secret Key is not
    # used. When unset, imageless events fall back to the gradient placeholders.
    unsplash_access_key: str | None = None
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
    # The "smtp" backend (app.services.email.SmtpEmailSender). Any SMTP server
    # works, for example a Gmail account with an app password
    # (smtp.gmail.com, port 587, SMTP_USERNAME = the address, SMTP_PASSWORD =
    # the app password, EMAIL_FROM = the same address). Port 465 uses TLS from
    # the start; any other port must offer STARTTLS.
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_timeout_seconds: float = 15.0

    # Sign in with a code sent by email (app.services.email_sign_in). Separate
    # from EMAIL_ENABLED, so it can be on while alert emails stay off. Offered
    # only when this is on AND EMAIL_BACKEND is a backend that really sends.
    email_sign_in_enabled: bool = False
    # Codes one address can be sent per hour, and codes one IP address can ask
    # for (to any addresses) per hour. Each new code invalidates the last.
    email_codes_per_address_per_hour: int = 5
    email_codes_per_ip_per_hour: int = 10
    # Wrong guesses allowed on one code before it stops working, and code
    # checks one IP address may make in 15 minutes.
    email_code_max_attempts: int = 5
    email_code_checks_per_ip_per_15_minutes: int = 30

    # External authentication (Phase 14). Each provider is independently
    # configured and is considered ENABLED only when it has a client id AND
    # secret — so the app starts fine with any or all disabled, and no provider
    # is ever offered without credentials. No real OAuth app is required for
    # development or tests (those use a mocked provider).
    # Where providers send people back to (…/auth/oauth/<provider>/callback).
    # Unset = the site's own address (PUBLIC_BASE_URL, which on Render is filled
    # in automatically). Each provider's app must list that callback URL.
    oauth_redirect_base_url: str | None = None
    google_client_id: str | None = None
    google_client_secret: str | None = None
    microsoft_client_id: str | None = None
    microsoft_client_secret: str | None = None
    facebook_client_id: str | None = None
    facebook_client_secret: str | None = None
    yahoo_client_id: str | None = None
    yahoo_client_secret: str | None = None
    x_client_id: str | None = None
    x_client_secret: str | None = None

    # Error monitoring (app.core.monitoring). Off unless SENTRY_DSN is set, so
    # development, tests and any deployment without it send nothing anywhere.
    # When set, crashes in web requests, failures in the scheduler's background
    # jobs and anything logged at ERROR are reported to Sentry. Performance
    # tracing samples only a small share of requests so a free Sentry plan's
    # monthly quota lasts; 0 turns tracing off.
    sentry_dsn: str | None = None
    sentry_traces_sample_rate: float = 0.02
    # Defaults to APP_ENV; set to tell apart e.g. a staging service.
    sentry_environment: str | None = None

    # Admin access key (app.services.auth). Off unless both are set. Lets an
    # automated job, such as the daily source-maintenance task, use the admin
    # pages without a browser sign-in by sending "Authorization: Bearer <key>".
    # Requests act as the named account, with its roles and in its audit
    # trail, and still need the usual CSRF token on forms. A key shorter than
    # 32 characters is ignored. Rotate by changing the value and redeploying.
    admin_api_key: str | None = None
    admin_api_key_user_email: str | None = None

    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @model_validator(mode="after")
    def _fill_from_platform(self) -> "Settings":
        """Fall back to the host platform's own address when nothing is set.

        Render sets RENDER_EXTERNAL_URL and RENDER_EXTERNAL_HOSTNAME on every
        web service. Without this, a deployment that never set PUBLIC_BASE_URL
        serves canonical links, Open Graph URLs and a sitemap that all name
        localhost, which tells search engines the real pages live at an
        unreachable address. An explicitly configured value always wins, so a
        custom domain overrides this.
        """
        external_url = os.environ.get("RENDER_EXTERNAL_URL", "").strip()
        if external_url and _is_local_url(self.public_base_url):
            self.public_base_url = external_url.rstrip("/")

        external_host = os.environ.get("RENDER_EXTERNAL_HOSTNAME", "").strip()
        if external_host and not self.trusted_hosts:
            self.trusted_hosts = [external_host]

        return self

    @model_validator(mode="after")
    def _no_test_login_in_production(self) -> "Settings":
        if self.test_login_enabled and self.app_env == "production":
            raise ValueError(
                "TEST_LOGIN_ENABLED lets anyone sign in without a password and "
                "cannot be used with APP_ENV=production."
            )
        return self

    @model_validator(mode="after")
    def _no_logged_sign_in_codes_in_production(self) -> "Settings":
        if (
            self.email_sign_in_enabled
            and self.email_backend == "console"
            and self.app_env == "production"
        ):
            raise ValueError(
                "EMAIL_BACKEND=console writes sign-in codes to the log and cannot be "
                "used with EMAIL_SIGN_IN_ENABLED in APP_ENV=production."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
