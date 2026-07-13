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
    categorization_rules,
    cities,
    event_categories,
    events,
    health,
    home,
    registration,
    websites,
)

settings = get_settings()
configure_logging(settings.log_level)
logger = get_logger("main")

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Phase 1: no scheduler, no scraping, no external network calls on startup.
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
app.include_router(health.router)
app.include_router(auth.router)
app.include_router(registration.router)
app.include_router(account.router)
app.include_router(admin.router)
app.include_router(cities.router)
app.include_router(websites.router)
app.include_router(events.router)
app.include_router(event_categories.router)
app.include_router(categorization_rules.router)
