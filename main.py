import sys
import asyncio

if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from contextlib import asynccontextmanager
from database import init_db, get_all_events
from scraper_runner import run_all_scrapers
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    init_db()
    await run_all_scrapers()  # Run once immediately on start

    scheduler.add_job(
        run_all_scrapers,
        trigger=IntervalTrigger(minutes=5),
        id="scrape_events",
        replace_existing=True,
        max_instances=1,
    )
    scheduler.start()
    logger.info("Scheduler started — scraping every 5 minutes")

    yield

    # Shutdown
    scheduler.shutdown()


app = FastAPI(title="City Events Aggregator", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    events = get_all_events()
    return templates.TemplateResponse("index.html", {"request": request, "events": events})


@app.get("/api/events")
async def api_events():
    return get_all_events()


@app.post("/api/refresh")
async def refresh():
    await run_all_scrapers()
    return {"status": "ok", "count": len(get_all_events())}
