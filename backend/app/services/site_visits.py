"""Visitor log: counts who visits the public site and which towns they look at.

Privacy first, and cheap enough for a small instance:

- No cookie is set and no IP address is stored. A visitor is a 32-character
  hash of (today's random salt, IP, user agent). The salt is deleted when the
  day ends, so a hash can't be turned back into an IP later and the same person
  can't be followed across days.
- Only real page views count: GET requests for public HTML pages that answered
  200. Static files, health checks, the admin, prefetches and anything that
  calls itself a bot or a script are skipped.
- Hits are added up in memory and written in one small batch every minute (or
  sooner when the buffer fills), not one database write per page view. At most
  a minute of counts is lost if the process restarts. The admin page flushes
  the buffer before reading, so it is always current.
- Old rows are deleted after `VISIT_RETENTION_DAYS`.

The middleware is `VisitTrackingMiddleware`; the admin page is
`/admin/visitors` (app.routers.visitors).
"""

from __future__ import annotations

import hashlib
import re
import secrets
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from urllib.parse import urlsplit

import anyio
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import get_settings
from app.core.logging import get_logger
from app.database import SessionLocal
from app.models.audit_log import AuditLog
from app.models.city import City
from app.models.site_visit import SitePageDay, SiteVisitorDay, VisitSalt
from app.models.user import User
from app.repositories.public_events import current_public_date

logger = get_logger("site_visits")

MAX_TOWNS_PER_VISITOR = 12
# Flush early when this many visitor or page entries are waiting, so memory
# stays small however busy the site gets.
MAX_BUFFERED = 500

_SKIPPED_PREFIXES = ("/static", "/admin", "/health", "/favicon", "/robots.txt", "/sitemap")
_BOT_PATTERN = re.compile(
    r"bot|crawl|spider|slurp|scrape|fetch|preview|monitor|uptime|headless|lighthouse"
    r"|facebookexternalhit|embedly|curl|wget|python|httpx|aiohttp|go-http|java/|okhttp"
    r"|node-fetch|axios|postman|insomnia|render",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------
# Deciding what counts
# --------------------------------------------------------------------------


def is_bot(user_agent: str) -> bool:
    return not user_agent.strip() or bool(_BOT_PATTERN.search(user_agent))


def device_for(user_agent: str) -> str:
    ua = user_agent.lower()
    if "ipad" in ua or "tablet" in ua:
        return "tablet"
    if "mobi" in ua or "android" in ua or "iphone" in ua:
        return "mobile"
    return "desktop"


def referrer_host(referer: str | None, own_hosts: set[str]) -> str | None:
    """The other site a visitor came from, or None for a direct visit or a
    click within this site."""
    if not referer:
        return None
    try:
        host = (urlsplit(referer).hostname or "").lower()
    except ValueError:
        return None
    if host.startswith("www."):
        host = host[4:]
    if not host or host in own_hosts:
        return None
    return host[:255]


def should_count_path(path: str) -> bool:
    return not path.startswith(_SKIPPED_PREFIXES)


def town_from_path(path: str) -> str | None:
    parts = path.strip("/").split("/")
    if len(parts) == 2 and parts[0] == "city" and parts[1]:
        return parts[1].lower()[:120]
    return None


# --------------------------------------------------------------------------
# Salt and hashing
# --------------------------------------------------------------------------

_salt_lock = threading.Lock()
_salt_cache: dict[date, str] = {}


def _salt_for(day: date) -> str:
    with _salt_lock:
        cached = _salt_cache.get(day)
        if cached is not None:
            return cached
        db = SessionLocal()
        try:
            row = db.get(VisitSalt, day)
            if row is None:
                db.add(VisitSalt(day=day, salt=secrets.token_hex(32)))
                try:
                    db.commit()
                except IntegrityError:
                    db.rollback()
                row = db.get(VisitSalt, day)
            salt = row.salt
            # Yesterday's salt goes: past hashes can no longer be recomputed.
            db.execute(delete(VisitSalt).where(VisitSalt.day < day))
            db.commit()
        finally:
            db.close()
        _salt_cache.clear()
        _salt_cache[day] = salt
        return salt


def visitor_hash(
    day: date,
    ip: str,
    user_agent: str,
    accept_language: str = "",
    *,
    user_id: int | None = None,
) -> str:
    """A signed-in visitor is keyed by their account; everyone else by IP,
    browser and language. Keying signed-in people by account keeps their
    visits apart from anonymous ones: before, a signed-out visit from the same
    network and browser (a private window, or anyone else on the same campus
    Wi-Fi with the same browser) was folded into the signed-in person's row and
    never showed as an anonymous visitor."""
    salt = _salt_for(day)
    source = f"user:{user_id}" if user_id is not None else f"{ip}|{user_agent}|{accept_language}"
    return hashlib.sha256(f"{salt}|{source}".encode()).hexdigest()[:32]


# --------------------------------------------------------------------------
# In-memory buffer
# --------------------------------------------------------------------------


@dataclass
class Hit:
    path: str
    ip: str
    user_agent: str
    referer: str | None
    host: str | None
    user_id: int | None
    town_slug: str | None
    accept_language: str = ""


@dataclass
class _VisitorEntry:
    page_views: int = 0
    device: str = "desktop"
    referrer_host: str | None = None
    towns: list[str] = field(default_factory=list)
    last_path: str | None = None
    user_id: int | None = None
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None


_buffer_lock = threading.Lock()
_flush_lock = threading.Lock()
_visitors: dict[tuple[date, str], _VisitorEntry] = {}
_pages: dict[tuple[date, str], list] = {}  # [views, town_slug]
_last_flush = time.monotonic()
_last_prune_day: date | None = None


def _own_hosts(host: str | None) -> set[str]:
    hosts = set()
    if host:
        hosts.add(host.split(":")[0].lower().removeprefix("www."))
    base = get_settings().public_base_url
    if base:
        base_host = (urlsplit(base).hostname or "").lower().removeprefix("www.")
        if base_host:
            hosts.add(base_host)
    return hosts


def record_hit(hit: Hit) -> None:
    day = current_public_date()
    now = datetime.now(UTC)
    key = (
        day,
        visitor_hash(
            day, hit.ip, hit.user_agent, hit.accept_language, user_id=hit.user_id
        ),
    )
    path = hit.path[:255]
    with _buffer_lock:
        entry = _visitors.get(key)
        if entry is None:
            entry = _VisitorEntry(
                device=device_for(hit.user_agent),
                referrer_host=referrer_host(hit.referer, _own_hosts(hit.host)),
                first_seen_at=now,
            )
            _visitors[key] = entry
        entry.page_views += 1
        entry.last_path = path
        entry.last_seen_at = now
        if hit.user_id is not None:
            entry.user_id = hit.user_id
        if hit.town_slug and hit.town_slug not in entry.towns:
            entry.towns.append(hit.town_slug)

        page = _pages.get((day, path))
        if page is None:
            _pages[(day, path)] = [1, hit.town_slug]
        else:
            page[0] += 1
            page[1] = page[1] or hit.town_slug


def pending_count() -> int:
    with _buffer_lock:
        return len(_visitors) + len(_pages)


def maybe_flush() -> None:
    interval = get_settings().visit_flush_seconds
    if time.monotonic() - _last_flush >= interval or pending_count() >= MAX_BUFFERED:
        flush()


def _merge_towns(existing: str | None, new: list[str]) -> str | None:
    towns = [t for t in (existing or "").split(",") if t]
    for town in new:
        if town not in towns:
            towns.append(town)
    towns = towns[:MAX_TOWNS_PER_VISITOR]
    joined = ",".join(towns)
    while len(joined) > 500:
        towns.pop()
        joined = ",".join(towns)
    return joined or None


def flush() -> None:
    """Write everything buffered to the database. Never raises: a failed
    write is logged and those counts are dropped rather than breaking a page."""
    global _visitors, _pages, _last_flush
    with _flush_lock:
        with _buffer_lock:
            visitors, pages = _visitors, _pages
            _visitors, _pages = {}, {}
            _last_flush = time.monotonic()
        if not visitors and not pages:
            _prune_if_due()
            return
        db = SessionLocal()
        try:
            _write(db, visitors, pages)
            db.commit()
        except Exception:
            db.rollback()
            logger.exception(
                "Visitor log: could not save %d visitors / %d pages", len(visitors), len(pages)
            )
        finally:
            db.close()
        _prune_if_due()


def _write(db: Session, visitors, pages) -> None:
    user_ids = {e.user_id for e in visitors.values() if e.user_id is not None}
    live_users = (
        set(db.scalars(select(User.id).where(User.id.in_(user_ids)))) if user_ids else set()
    )
    for day in {d for d, _ in visitors}:
        hashes = [h for d, h in visitors if d == day]
        existing = {
            row.visitor_hash: row
            for row in db.scalars(
                select(SiteVisitorDay).where(
                    SiteVisitorDay.day == day, SiteVisitorDay.visitor_hash.in_(hashes)
                )
            )
        }
        for visitor in hashes:
            entry = visitors[(day, visitor)]
            user_id = entry.user_id if entry.user_id in live_users else None
            row = existing.get(visitor)
            if row is None:
                db.add(
                    SiteVisitorDay(
                        day=day,
                        visitor_hash=visitor,
                        user_id=user_id,
                        page_views=entry.page_views,
                        device=entry.device,
                        referrer_host=entry.referrer_host,
                        towns=_merge_towns(None, entry.towns),
                        last_path=entry.last_path,
                        first_seen_at=entry.first_seen_at,
                        last_seen_at=entry.last_seen_at,
                    )
                )
            else:
                row.page_views += entry.page_views
                row.towns = _merge_towns(row.towns, entry.towns)
                row.last_path = entry.last_path
                row.last_seen_at = entry.last_seen_at
                if user_id is not None:
                    row.user_id = user_id
                if row.referrer_host is None:
                    row.referrer_host = entry.referrer_host

    for day in {d for d, _ in pages}:
        paths = [p for d, p in pages if d == day]
        existing_pages = {
            row.path: row
            for row in db.scalars(
                select(SitePageDay).where(SitePageDay.day == day, SitePageDay.path.in_(paths))
            )
        }
        for path in paths:
            views, town = pages[(day, path)]
            row = existing_pages.get(path)
            if row is None:
                db.add(SitePageDay(day=day, path=path, town_slug=town, views=views))
            else:
                row.views += views
                row.town_slug = row.town_slug or town


def _prune_if_due() -> None:
    global _last_prune_day
    today = current_public_date()
    if _last_prune_day == today:
        return
    _last_prune_day = today
    cutoff = today - timedelta(days=get_settings().visit_retention_days)
    db = SessionLocal()
    try:
        db.execute(delete(SiteVisitorDay).where(SiteVisitorDay.day < cutoff))
        db.execute(delete(SitePageDay).where(SitePageDay.day < cutoff))
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Visitor log: could not delete old rows")
    finally:
        db.close()


def reset_for_tests() -> None:
    global _visitors, _pages, _last_prune_day
    with _buffer_lock:
        _visitors, _pages = {}, {}
    _salt_cache.clear()
    _last_prune_day = None


# --------------------------------------------------------------------------
# Middleware
# --------------------------------------------------------------------------


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers") or ():
        if key == name:
            return value.decode("latin-1")
    return None


def _client_ip(scope: Scope) -> str:
    # Render's proxy puts the visitor's address first in X-Forwarded-For. It
    # is only hashed, never stored or trusted for anything else, so a forged
    # value can at worst make one person count as two.
    forwarded = _header(scope, b"x-forwarded-for")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    client = scope.get("client")
    return client[0] if client else ""


class VisitTrackingMiddleware:
    """Counts public page views after the response has gone out, so it never
    slows a page down. Pure ASGI: the body is passed straight through."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["method"] != "GET"
            or not get_settings().visit_tracking_enabled
            or not should_count_path(scope["path"])
        ):
            await self.app(scope, receive, send)
            return

        # Routes record the signed-in user and the town on request.state; make
        # sure the dict exists now so copies of the scope further in share it.
        state = scope.setdefault("state", {})
        result: dict = {}

        async def watch(message: Message) -> None:
            if message["type"] == "http.response.start":
                result["status"] = message["status"]
                for key, value in message.get("headers") or ():
                    if key.lower() == b"content-type":
                        result["html"] = value.startswith(b"text/html")
            await send(message)

        await self.app(scope, receive, watch)

        if result.get("status") != 200 or not result.get("html"):
            return
        user_agent = _header(scope, b"user-agent") or ""
        if is_bot(user_agent):
            return
        purpose = (_header(scope, b"sec-purpose") or _header(scope, b"purpose") or "").lower()
        if "prefetch" in purpose:
            return
        if state.get("authenticated_by_api_key"):
            return
        hit = Hit(
            path=scope["path"],
            ip=_client_ip(scope),
            user_agent=user_agent[:500],
            accept_language=(_header(scope, b"accept-language") or "")[:200],
            referer=_header(scope, b"referer"),
            host=_header(scope, b"host"),
            user_id=state.get("visit_user_id"),
            town_slug=state.get("visit_town") or town_from_path(scope["path"]),
        )
        try:
            await anyio.to_thread.run_sync(_record_and_maybe_flush, hit)
        except Exception:
            logger.exception("Visitor log: could not record a page view")


def _record_and_maybe_flush(hit: Hit) -> None:
    record_hit(hit)
    maybe_flush()


# --------------------------------------------------------------------------
# Admin report
# --------------------------------------------------------------------------

LOG_PAGE_SIZE = 50


@dataclass
class DayRow:
    day: date
    visitors: int = 0
    page_views: int = 0
    signed_in: int = 0


def _town_names(db: Session, slugs: set[str]) -> dict[str, str]:
    if not slugs:
        return {}
    rows = db.execute(select(City.slug, City.name, City.state_or_region).where(
        City.slug.in_(slugs)
    ))
    return {
        slug: f"{name}, {state}" if state else name for slug, name, state in rows
    }


def build_visitor_report(db: Session, *, days: int, log_page: int = 1) -> dict:
    flush()
    today = current_public_date()
    start = today - timedelta(days=days - 1)

    daily = {start + timedelta(days=i): DayRow(day=start + timedelta(days=i)) for i in range(days)}
    for day, visitors, views, signed_in in db.execute(
        select(
            SiteVisitorDay.day,
            func.count(SiteVisitorDay.id),
            func.coalesce(func.sum(SiteVisitorDay.page_views), 0),
            func.count(SiteVisitorDay.user_id),
        )
        .where(SiteVisitorDay.day >= start)
        .group_by(SiteVisitorDay.day)
    ):
        if day in daily:
            daily[day] = DayRow(day=day, visitors=visitors, page_views=views, signed_in=signed_in)
    daily_rows = sorted(daily.values(), key=lambda r: r.day, reverse=True)

    # Towns: views from the page table, distinct visitors from the visitor table.
    town_views: Counter[str] = Counter()
    for slug, views in db.execute(
        select(SitePageDay.town_slug, func.sum(SitePageDay.views))
        .where(SitePageDay.day >= start, SitePageDay.town_slug.is_not(None))
        .group_by(SitePageDay.town_slug)
    ):
        town_views[slug] = int(views or 0)
    town_visitors: Counter[str] = Counter()
    for (towns,) in db.execute(
        select(SiteVisitorDay.towns).where(
            SiteVisitorDay.day >= start, SiteVisitorDay.towns.is_not(None)
        )
    ):
        for slug in towns.split(","):
            if slug:
                town_visitors[slug] += 1

    top_pages = db.execute(
        select(SitePageDay.path, func.sum(SitePageDay.views).label("views"))
        .where(SitePageDay.day >= start)
        .group_by(SitePageDay.path)
        .order_by(func.sum(SitePageDay.views).desc(), SitePageDay.path)
        .limit(15)
    ).all()

    referrers = db.execute(
        select(SiteVisitorDay.referrer_host, func.count(SiteVisitorDay.id))
        .where(SiteVisitorDay.day >= start, SiteVisitorDay.referrer_host.is_not(None))
        .group_by(SiteVisitorDay.referrer_host)
        .order_by(func.count(SiteVisitorDay.id).desc(), SiteVisitorDay.referrer_host)
        .limit(10)
    ).all()

    devices = dict(
        db.execute(
            select(SiteVisitorDay.device, func.count(SiteVisitorDay.id))
            .where(SiteVisitorDay.day >= start)
            .group_by(SiteVisitorDay.device)
        ).all()
    )

    log_page = max(log_page, 1)
    log_rows = db.execute(
        select(SiteVisitorDay, User)
        .outerjoin(User, User.id == SiteVisitorDay.user_id)
        .where(SiteVisitorDay.day >= start)
        .order_by(SiteVisitorDay.last_seen_at.desc(), SiteVisitorDay.id.desc())
        .offset((log_page - 1) * LOG_PAGE_SIZE)
        .limit(LOG_PAGE_SIZE + 1)
    ).all()
    log_has_next = len(log_rows) > LOG_PAGE_SIZE
    log_rows = log_rows[:LOG_PAGE_SIZE]

    sign_ins = db.execute(
        select(AuditLog, User)
        .outerjoin(User, User.id == AuditLog.user_id)
        .where(AuditLog.action.in_(("login", "login_failed")))
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(20)
    ).all()

    slugs = set(town_views) | set(town_visitors)
    for row, _ in log_rows:
        slugs.update(t for t in (row.towns or "").split(",") if t)
    names = _town_names(db, slugs)

    def town_label(slug: str) -> str:
        return names.get(slug, slug)

    towns = sorted(
        (
            {
                "slug": slug,
                "name": town_label(slug),
                "visitors": town_visitors.get(slug, 0),
                "views": town_views.get(slug, 0),
            }
            for slug in slugs & (set(town_views) | set(town_visitors))
        ),
        key=lambda t: (-t["visitors"], -t["views"], t["name"]),
    )[:20]

    log = [
        {
            "last_seen_at": row.last_seen_at,
            "first_seen_at": row.first_seen_at,
            "user": user,
            "page_views": row.page_views,
            "towns": [
                {"slug": s, "name": town_label(s)} for s in (row.towns or "").split(",") if s
            ],
            "device": row.device,
            "referrer_host": row.referrer_host,
            "last_path": row.last_path,
        }
        for row, user in log_rows
    ]

    today_row = daily.get(today, DayRow(day=today))
    return {
        "days": days,
        "today": today_row,
        "total_visits": sum(r.visitors for r in daily_rows),
        "total_page_views": sum(r.page_views for r in daily_rows),
        "signed_in_people": db.scalar(
            select(func.count(func.distinct(SiteVisitorDay.user_id))).where(
                SiteVisitorDay.day >= start
            )
        )
        or 0,
        "daily": daily_rows,
        "towns": towns,
        "top_pages": [{"path": p, "views": int(v or 0)} for p, v in top_pages],
        "referrers": [{"host": h, "visitors": c} for h, c in referrers],
        "devices": {k: devices.get(k, 0) for k in ("mobile", "desktop", "tablet")},
        "log": log,
        "log_page": log_page,
        "log_has_next": log_has_next,
        "sign_ins": [
            {"at": entry.created_at, "user": user, "action": entry.action, "detail": entry.detail}
            for entry, user in sign_ins
        ],
        "retention_days": get_settings().visit_retention_days,
    }
