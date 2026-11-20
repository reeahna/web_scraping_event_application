"""robots.txt and sitemap.xml.

Both are public and unauthenticated by nature. They deliberately expose only
what the public site already serves: the home page, each active city, and
upcoming public events. Nothing under /admin or /account appears, and robots.txt
disallows those prefixes so a crawler that finds a link does not follow it.
"""

from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import select

from app.config import get_settings
from app.core.exceptions import NotFoundError
from app.dependencies import DbSession
from app.models.base import as_aware_utc
from app.models.city import City
from app.paths import STATIC_DIR
from app.repositories.city import get_city_by_slug, get_city_by_slug_ignoring_case
from app.repositories.public_events import (
    list_public_events,
    local_today,
    public_event_sitemap_rows,
    town_today,
    upcoming_counts_by_city,
)
from app.services.calendar_feed import town_calendar
from app.services.seo import TOWN_WINDOWS, absolute_url, town_is_indexable

router = APIRouter(tags=["seo"])

# Everything a signed-in user sees. Crawling these wastes crawl budget on pages
# that redirect to a login form, and a private area has no business in an index.
DISALLOWED_PREFIXES = ("/admin", "/account", "/auth", "/register")

# A sitemap file may hold 50,000 URLs (and 50 MB). The old cap of 10,000 events
# left about 3,000 upcoming events out once the catalogue grew past it; this
# keeps one file, with room to spare, until the catalogue needs a sitemap index.
MAX_SITEMAP_URLS = 45_000


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
    # No trailing slash, so the bare paths (/admin, /account) are covered too.
    lines += [f"Disallow: {prefix}" for prefix in DISALLOWED_PREFIXES]
    lines.append(f"Sitemap: {absolute_url('/sitemap.xml')}")
    return Response("\n".join(lines) + "\n", media_type="text/plain")


@router.get("/sitemap.xml", include_in_schema=False)
def sitemap_xml(db: DbSession) -> Response:
    today = local_today(db)
    urls = [
        absolute_url("/"),
        absolute_url("/whats-new"),
        absolute_url("/privacy"),
        absolute_url("/terms"),
    ]

    # Only towns with enough upcoming events: an empty town page is marked
    # noindex, and a sitemap should list only pages meant for the index.
    # Each indexable town also has a page for this weekend and one for today,
    # the windows people search for ("things to do in Ann Arbor this weekend").
    counts = upcoming_counts_by_city(db, today=today)
    for city_id, slug in db.execute(
        select(City.id, City.slug).where(City.is_active.is_(True)).order_by(City.slug)
    ):
        if town_is_indexable(counts.get(city_id, 0)):
            urls += [
                absolute_url(f"/city/{slug}{suffix}")
                for suffix in ("", *(f"/{window}" for window in TOWN_WINDOWS))
            ]

    # The same visibility rules as the public listing, so the sitemap can never
    # advertise an event the site would not show.
    # Each event carries <lastmod>: when its listed details last changed
    # (content_updated_at), so a crawler revisits what changed rather than
    # everything. Left out when unknown: a guessed date is worse than none.
    entries = [(url, None) for url in urls]
    entries += [
        (absolute_url(f"/events/{event_id}"), updated)
        for event_id, updated in public_event_sitemap_rows(
            db, today=today, limit=MAX_SITEMAP_URLS - len(urls)
        )
    ]

    body = "\n".join(
        ['<?xml version="1.0" encoding="UTF-8"?>',
         '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
        + [_url_entry(url, updated) for url, updated in entries]
        + ["</urlset>"]
    )
    return Response(body, media_type="application/xml")


def _url_entry(url: str, updated) -> str:
    if updated is None:
        return f"  <url><loc>{url}</loc></url>"
    lastmod = as_aware_utc(updated).isoformat(timespec="seconds")
    return f"  <url><loc>{url}</loc><lastmod>{lastmod}</lastmod></url>"


@router.get("/city/{slug}/events.ics", include_in_schema=False)
def town_calendar_feed(slug: str, request: Request, db: DbSession) -> Response:
    """A town's upcoming events as an iCalendar feed (subscribe in a calendar
    app, or read with any program). The same events the town page lists."""
    city = get_city_by_slug(db, slug)
    if city is None:
        canonical = get_city_by_slug_ignoring_case(db, slug)
        if canonical is not None and canonical.is_active:
            query = f"?{request.url.query}" if request.url.query else ""
            return RedirectResponse(
                f"/city/{canonical.slug}/events.ics{query}", status_code=301
            )
    if city is None or not city.is_active:
        raise NotFoundError("City not found")
    events, _, _ = list_public_events(
        db, today=town_today(city), city_id=city.id, per_page=MAX_FEED_EVENTS
    )
    return Response(
        town_calendar(city, events),
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": f'inline; filename="{city.slug}-events.ics"'},
    )


@router.get("/favicon.ico", include_in_schema=False)
def favicon() -> FileResponse:
    """Browsers and search engines ask for /favicon.ico whatever the page
    links to; answer with the site icon instead of a 404. Served directly
    rather than redirected, so it costs one request."""
    return FileResponse(
        STATIC_DIR / "favicon.ico",
        media_type="image/x-icon",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/llms.txt", include_in_schema=False)
def llms_txt(db: DbSession) -> Response:
    """A plain-text guide to the site for AI tools (the llms.txt convention):
    what it is, and where the towns, feeds and sitemap are."""
    settings = get_settings()
    counts = upcoming_counts_by_city(db, today=local_today(db))
    cities = db.scalars(select(City).where(City.is_active.is_(True)).order_by(City.name)).all()

    lines = [
        f"# {settings.app_name}",
        "",
        f"> {settings.public_tagline} {settings.app_name} lists upcoming public events "
        "in college towns (concerts, theater, festivals, talks, sports and campus events), "
        "gathered from local venues, universities and tourism sites. Every event links to "
        "its original source.",
        "",
        "Each town has a page of upcoming events (plus /this-weekend and /today pages), "
        "and the same events as an iCalendar feed. Each event page carries schema.org "
        "Event data (JSON-LD). Listings change daily; always confirm details with the organizer.",
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
