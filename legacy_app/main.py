import sys
import asyncio

if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from contextlib import asynccontextmanager
from database import init_db, get_all_events, get_event_count
from scraper_runner import run_all_scrapers, run_city_scrapers, CITIES
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    await run_all_scrapers()

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

    scheduler.shutdown()


app = FastAPI(title="City Events Aggregator", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    cities = [
        {"slug": slug, "name": info["name"], "count": get_event_count(city=info["name"])}
        for slug, info in CITIES.items()
    ]
    return templates.TemplateResponse("home.html", {"request": request, "cities": cities})


@app.get("/city/{slug}", response_class=HTMLResponse)
async def city_page(request: Request, slug: str):
    city_info = CITIES.get(slug)
    if not city_info:
        raise HTTPException(status_code=404, detail="City not found")
    city_name = city_info["name"]
    events = get_all_events(city=city_name)
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "events": events, "city_name": city_name, "city_slug": slug},
    )


@app.get("/api/events")
async def api_events(city: str | None = None):
    return get_all_events(city=city)


@app.post("/api/refresh/{slug}")
async def refresh_city(slug: str):
    city_info = CITIES.get(slug)
    if not city_info:
        raise HTTPException(status_code=404, detail="City not found")
    count = await run_city_scrapers(slug)
    events = get_all_events(city=city_info["name"])
    return {"status": "ok", "count": len(events)}


@app.post("/api/refresh")
async def refresh_all():
    await run_all_scrapers()
    return {"status": "ok", "count": len(get_all_events())}


@app.post("/api/geocode")
async def trigger_geocode(limit: int = 150):
    from geocoder import geocode_new_events
    import asyncio
    asyncio.create_task(geocode_new_events(limit=limit))
    return {"status": "started", "limit": limit}
