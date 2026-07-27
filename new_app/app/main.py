from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.core.exceptions import (
    AppError,
    NotAuthenticatedError,
    app_error_handler,
    not_authenticated_handler,
    unhandled_exception_handler,
)
from app.core.logging import configure_logging, get_logger
from app.core.middleware import CorrelationIdMiddleware
from app.routers import (
    account,
    admin,
    auth,
    auto_onboarding_policies,
    categorization_rules,
    cities,
    engagement,
    event_categories,
    events,
    geocoding,
    health,
    home,
    notifications,
    oauth,
    onboarding,
    public_events,
    registration,
    reporting,
    scheduler,
    unsupported_reports,
    websites,
)

settings = get_settings()
configure_logging(settings.log_level)
logger = get_logger("main")

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # The web process deliberately starts NO scheduler and makes no external
    # network calls on startup. Background scraping runs in a separate dedicated
    # scheduler process (`python -m app.scheduler`), never per web worker — see
    # app.scheduler and docs/scheduler.md.
    logger.info("New app starting up (env=%s, port=%s)", settings.app_env, settings.app_port)
    yield
    logger.info("New app shutting down")


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.add_middleware(CorrelationIdMiddleware)

app.add_exception_handler(AppError, app_error_handler)
app.add_exception_handler(NotAuthenticatedError, not_authenticated_handler)
app.add_exception_handler(Exception, unhandled_exception_handler)

app.include_router(home.router)
app.include_router(public_events.router)
app.include_router(health.router)
app.include_router(auth.router)
app.include_router(oauth.router)
app.include_router(registration.router)
app.include_router(account.router)
app.include_router(admin.router)
app.include_router(cities.router)
# Registered before the websites router so /admin/websites/onboard is
# matched by the onboarding route rather than by /admin/websites/{website_id}.
app.include_router(onboarding.router)
app.include_router(websites.router)
app.include_router(events.router)
app.include_router(event_categories.router)
app.include_router(categorization_rules.router)
app.include_router(unsupported_reports.router)
app.include_router(notifications.router)
app.include_router(auto_onboarding_policies.router)
app.include_router(auto_onboarding_policies.decisions_router)
app.include_router(scheduler.router)
app.include_router(geocoding.router)
app.include_router(engagement.router)
app.include_router(reporting.router)
