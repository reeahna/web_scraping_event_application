import asyncio
import logging
import re

import asyncio as _asyncio
from database import upsert_event, export_csv, get_conn
from scrapers.bloomington_in.iu_events import IUEventsScraper
from scrapers.bloomington_in.parks import BloomingtonParksScraper
from scrapers.bloomington_in.eventbrite import EventbriteScraper
from scrapers.bloomington_in.bloomingtonian import BloomingtonianScraper
from scrapers.bloomington_in.visit_bloomington import VisitBloomingtonScraper
from scrapers.bethlehem_pa.discover_lehigh_valley import BethlehemScraper
from scrapers.bethlehem_pa.the_events_calendar import TheEventsCalendarScraper
from scrapers.bethlehem_pa.wind_creek import WindCreekScraper

logger = logging.getLogger(__name__)


def city_to_slug(name: str) -> str:
    return re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')


CITIES: dict[str, dict] = {
    "bloomington-in": {
        "name": "Bloomington Area, IN",
        "scrapers": [
            IUEventsScraper(),
            BloomingtonParksScraper(),
            EventbriteScraper(),
            VisitBloomingtonScraper(),
        ],
    },
    "bethlehem-pa": {
        "name": "Bethlehem, PA",
        "scrapers": [
            BethlehemScraper(),
            TheEventsCalendarScraper(
                name="City of Bethlehem Events",
                base_url="https://www.bethlehempa.org/events/",
                city="Bethlehem, PA",
            ),
            TheEventsCalendarScraper(
                name="Visit Historic Bethlehem",
                base_url="https://www.visithistoricbethlehem.com/events/list/",
                city="Bethlehem, PA",
            ),
            WindCreekScraper(),
            EventbriteScraper(
                base_url="https://www.eventbrite.com/d/pa--bethlehem/events/",
                city="Bethlehem, PA",
                allowed_localities={"Bethlehem", "Hellertown", "Northampton"},
            ),
        ],
    },
}

_iu_scraper_ref = CITIES["bloomington-in"]["scrapers"][0]


def _purge_internal_iu_events() -> None:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, title, description FROM events WHERE source = ?",
            ("IU Bloomington Calendar",),
        ).fetchall()
        to_delete = [
            row["id"] for row in rows
            if _iu_scraper_ref._is_internal(row["title"], row["description"])
        ]
        if to_delete:
            conn.execute(
                f"DELETE FROM events WHERE id IN ({','.join('?' * len(to_delete))})",
                to_delete,
            )
            conn.commit()
            logger.info(f"Purged {len(to_delete)} internal IU events from DB")


async def run_city_scrapers(city_slug: str) -> int:
    city_info = CITIES.get(city_slug)
    if not city_info:
        logger.error(f"Unknown city slug: {city_slug}")
        return 0

    city_name = city_info["name"]
    scrapers = city_info["scrapers"]
    logger.info(f"Scraping {city_name}...")

    results = await asyncio.gather(
        *[s.scrape() for s in scrapers],
        return_exceptions=True,
    )

    total = 0
    for scraper, result in zip(scrapers, results):
        if isinstance(result, Exception):
            logger.error(f"[{scraper.name}] Scraper crashed: {result}")
            continue
        for event in result:
            upsert_event(
                title=event.title,
                url=event.url,
                source=event.source,
                description=event.description,
                date=event.date,
                end_date=event.end_date,
                time=event.time,
                venue=event.venue,
                address=event.address,
                image_url=event.image_url,
                category=event.category,
                city=event.city or city_name,
            )
        total += len(result)
        logger.info(f"[{scraper.name}] Saved {len(result)} events")

    return total


async def run_all_scrapers() -> None:
    logger.info("Starting scrape run...")
    total = 0
    for city_slug in CITIES:
        total += await run_city_scrapers(city_slug)
    logger.info(f"Scrape run complete. Total events saved: {total}")
    _purge_internal_iu_events()
    export_csv()
    # Geocode new events in the background so it doesn't block the scrape response
    from geocoder import geocode_new_events
    _asyncio.create_task(geocode_new_events(limit=150))
