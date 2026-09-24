"""robots.txt and sitemap.xml.

Both are public and unauthenticated by nature. They deliberately expose only
what the public site already serves: the home page, each active city, and
upcoming public events. Nothing under /admin or /account appears, and robots.txt
disallows those prefixes so a crawler that finds a link does not follow it.
"""

from fastapi import APIRouter, Response
from sqlalchemy import select

from app.dependencies import DbSession
from app.models.city import City
from app.repositories.public_events import current_public_date, public_event_ids
from app.services.seo import absolute_url

router = APIRouter(tags=["seo"])

# Everything a signed-in user sees. Crawling these wastes crawl budget on pages
# that redirect to a login form, and a private area has no business in an index.
DISALLOWED_PREFIXES = ("/admin", "/account", "/auth", "/register")

# A cap, so a site with a very large catalogue still returns one valid document
# rather than a multi-megabyte response. Sitemaps may hold 50,000 URLs.
MAX_SITEMAP_URLS = 10_000


@router.get("/robots.txt", include_in_schema=False)
def robots_txt() -> Response:
    lines = ["User-agent: *"]
    lines += [f"Disallow: {prefix}/" for prefix in DISALLOWED_PREFIXES]
    lines.append(f"Sitemap: {absolute_url('/sitemap.xml')}")
    return Response("\n".join(lines) + "\n", media_type="text/plain")


@router.get("/sitemap.xml", include_in_schema=False)
def sitemap_xml(db: DbSession) -> Response:
    today = current_public_date()
    urls = [absolute_url("/")]

    urls += [
        absolute_url(f"/city/{slug}")
        for slug in db.scalars(
            select(City.slug).where(City.is_active.is_(True)).order_by(City.slug)
        )
    ]

    # The same visibility rules as the public listing, so the sitemap can never
    # advertise an event the site would not show.
    urls += [
        absolute_url(f"/events/{event_id}")
        for event_id in public_event_ids(db, today=today, limit=MAX_SITEMAP_URLS)
    ]

    body = "\n".join(
        ['<?xml version="1.0" encoding="UTF-8"?>',
         '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
        + [f"  <url><loc>{url}</loc></url>" for url in urls]
        + ["</urlset>"]
    )
    return Response(body, media_type="application/xml")
