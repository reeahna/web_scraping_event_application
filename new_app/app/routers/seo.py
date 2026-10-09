"""robots.txt and sitemap.xml.

Both are public and unauthenticated by nature. They deliberately expose only
what the public site already serves: the home page, each active city, and
upcoming public events. Nothing under /admin or /account appears, and robots.txt
disallows those prefixes so a crawler that finds a link does not follow it.
"""

from fastapi import APIRouter, Response
from sqlalchemy import select

from app.config import get_settings
from app.core.exceptions import NotFoundError
from app.dependencies import DbSession
from app.models.city import City
from app.repositories.city import get_city_by_slug
from app.repositories.public_events import (
    current_public_date,
    list_public_events,
    public_event_ids,
    upcoming_counts_by_city,
)
from app.services.calendar_feed import town_calendar
from app.services.seo import absolute_url

router = APIRouter(tags=["seo"])

# Everything a signed-in user sees. Crawling these wastes crawl budget on pages
# that redirect to a login form, and a private area has no business in an index.
DISALLOWED_PREFIXES = ("/admin", "/account", "/auth", "/register")

# A cap, so a site with a very large catalogue still returns one valid document
# rather than a multi-megabyte response. Sitemaps may hold 50,000 URLs.
MAX_SITEMAP_URLS = 10_000


# AI crawlers, named so it is explicit that they are welcome on the public
# pages. They share ONE group with "*": a crawler that finds a group naming it
# ignores the "*" group, so a separate group would have to repeat every
# Disallow line or it would let that crawler into /admin and /account.
# Each company has a crawler for training and others for search and for
# fetching a page when a user asks about it.
AI_CRAWLERS = (
    "GPTBot",  # OpenAI: training
    "OAI-SearchBot",  # OpenAI: ChatGPT search
    "ChatGPT-User",  # OpenAI: a page fetched for a user
    "ClaudeBot",  # Anthropic: training
    "Claude-SearchBot",  # Anthropic: Claude search
    "Claude-User",  # Anthropic: a page fetched for a user
    "Google-Extended",  # Google: Gemini (Googlebot already covers search)
    "PerplexityBot",  # Perplexity: search
    "Perplexity-User",  # Perplexity: a page fetched for a user
    "Applebot-Extended",  # Apple: Apple Intelligence
)

# Upcoming events per feed. Generous for one town, and bounded.
MAX_FEED_EVENTS = 1000


@router.get("/robots.txt", include_in_schema=False)
def robots_txt() -> Response:
    lines = [f"User-agent: {name}" for name in AI_CRAWLERS] + ["User-agent: *"]
    lines += [f"Disallow: {prefix}/" for prefix in DISALLOWED_PREFIXES]
    lines.append(f"Sitemap: {absolute_url('/sitemap.xml')}")
    return Response("\n".join(lines) + "\n", media_type="text/plain")


@router.get("/sitemap.xml", include_in_schema=False)
def sitemap_xml(db: DbSession) -> Response:
    today = current_public_date()
    urls = [absolute_url("/"), absolute_url("/privacy"), absolute_url("/terms")]

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


@router.get("/city/{slug}/events.ics", include_in_schema=False)
def town_calendar_feed(slug: str, db: DbSession) -> Response:
    """A town's upcoming events as an iCalendar feed (subscribe in a calendar
    app, or read with any program). The same events the town page lists."""
    city = get_city_by_slug(db, slug)
    if city is None or not city.is_active:
        raise NotFoundError("City not found")
    events, _, _ = list_public_events(
        db, today=current_public_date(), city_id=city.id, per_page=MAX_FEED_EVENTS
    )
    return Response(
        town_calendar(city, events),
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": f'inline; filename="{city.slug}-events.ics"'},
    )


@router.get("/llms.txt", include_in_schema=False)
def llms_txt(db: DbSession) -> Response:
    """A plain-text guide to the site for AI tools (the llms.txt convention):
    what it is, and where the towns, feeds and sitemap are."""
    settings = get_settings()
    counts = upcoming_counts_by_city(db, today=current_public_date())
    cities = db.scalars(select(City).where(City.is_active.is_(True)).order_by(City.name)).all()

    lines = [
        f"# {settings.app_name}",
        "",
        f"> {settings.public_tagline} {settings.app_name} lists upcoming public events "
        "in college towns (concerts, theater, festivals, talks, sports and campus events), "
        "gathered from local venues, universities and tourism sites. Every event links to "
        "its original source.",
        "",
        "Each town has a page of upcoming events, and the same events as an iCalendar "
        "feed. Each event page carries schema.org Event data (JSON-LD). Listings change "
        "daily; always confirm details with the organizer.",
        "",
        "## Towns",
        "",
    ]
    for city in cities:
        schools = ", ".join(u.name for u in city.universities)
        where = ", ".join(p for p in (city.state_or_region, schools) if p)
        lines.append(
            f"- [{city.name}]({absolute_url(f'/city/{city.slug}')})"
            + (f" ({where})" if where else "")
            + f": {counts.get(city.id, 0)} upcoming event"
            + ("" if counts.get(city.id, 0) == 1 else "s")
            + ";"
            f" [calendar feed]({absolute_url(f'/city/{city.slug}/events.ics')})"
        )
    lines += [
        "",
        "## Site",
        "",
        f"- [Sitemap]({absolute_url('/sitemap.xml')}): every town and upcoming event page",
        f"- [Terms of use]({absolute_url('/terms')}): reading and indexing public pages "
        "is welcome",
        f"- [Privacy policy]({absolute_url('/privacy')})",
        "",
    ]
    return Response("\n".join(lines), media_type="text/plain; charset=utf-8")
