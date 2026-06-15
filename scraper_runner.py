import asyncio
import logging

from database import upsert_event, export_csv, get_conn
from scrapers.eventbrite import EventbriteScraper
from scrapers.bloomington_parks import BloomingtonParksScraper
from scrapers.iu_events import IUEventsScraper

logger = logging.getLogger(__name__)

_iu_scraper_ref = IUEventsScraper()


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


SCRAPERS = [
    IUEventsScraper(),
    BloomingtonParksScraper(),
    EventbriteScraper(),
]


async def run_all_scrapers():
    logger.info("Starting scrape run...")
    results = await asyncio.gather(
        *[scraper.scrape() for scraper in SCRAPERS],
        return_exceptions=True,
    )

    total = 0
    for scraper, result in zip(SCRAPERS, results):
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
            )
        total += len(result)
        logger.info(f"[{scraper.name}] Saved {len(result)} events")

    logger.info(f"Scrape run complete. Total events saved: {total}")
    _purge_internal_iu_events()
    export_csv()