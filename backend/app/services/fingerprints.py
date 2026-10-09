import hashlib
import html
import re
import unicodedata
from datetime import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.event import Event

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_text(value: str | None) -> str:
    return _WHITESPACE_RE.sub(" ", (value or "").strip()).casefold()


_NON_WORD_RE = re.compile(r"[^a-z0-9]+")


def title_key(value: str | None) -> str:
    """A title reduced to its words, for spotting the same event listed by two
    sources: "Heist!" and "HEIST", or "Rock &amp; Roll Night" and "Rock and
    Roll Night", come out the same. Stored as Event.normalized_title."""
    value = html.unescape(value or "").replace("&", " and ")
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return " ".join(_NON_WORD_RE.sub(" ", value.casefold()).split())


def normalize_url(value: str | None) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    parsed = urlsplit(value)
    host = (parsed.hostname or "").casefold()
    port = f":{parsed.port}" if parsed.port else ""
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
    return urlunsplit((parsed.scheme.casefold(), f"{host}{port}", path, query, ""))


def event_fingerprint(event: Event) -> str:
    if event.website_id is not None and event.external_source_id:
        identity = f"external|{event.website_id}|{event.external_source_id.strip()}"
    elif normalize_url(event.canonical_url):
        identity = f"url|{normalize_url(event.canonical_url)}"
    else:
        date_part = event.start_date.isoformat() if event.start_date else ""
        time_part = event.start_time.isoformat() if event.start_time else ""
        identity = "|".join(
            (
                "composite",
                normalize_text(event.title),
                date_part,
                time_part,
                normalize_text(event.venue),
                str(event.city_id or ""),
            )
        )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _time_unknown(column):
    # Several sources give midnight when they have no time at all.
    return or_(column.is_(None), column == time(0, 0))


def same_listing_events(db: Session, event: Event) -> list[Event]:
    """Other events with the same title (by title_key) on the same day in the
    same city, at the same time or with no time on either side: usually the
    same event listed by another source under its own URL, so its fingerprint
    differs. The public site shows one of them (see
    app.repositories.public_events._without_repeat_listings); this is what
    flags them for an admin."""
    if not event.normalized_title or event.start_date is None or event.city_id is None:
        return []
    query = db.query(Event).filter(
        Event.id != event.id,
        Event.normalized_title == event.normalized_title,
        Event.start_date == event.start_date,
        Event.city_id == event.city_id,
        Event.is_recurrence_parent.is_(False),
    )
    if event.start_time is not None and event.start_time != time(0, 0):
        query = query.filter(
            or_(_time_unknown(Event.start_time), Event.start_time == event.start_time)
        )
    return query.order_by(Event.id).all()


def update_fingerprint_and_duplicates(db: Session, event: Event) -> list[Event]:
    event.normalized_title = title_key(event.title)
    event.fingerprint = event_fingerprint(event)
    db.flush()
    matches = (
        db.query(Event)
        .filter(Event.id != event.id, Event.fingerprint == event.fingerprint)
        .order_by(Event.id)
        .all()
    )
    seen = {m.id for m in matches}
    matches += [m for m in same_listing_events(db, event) if m.id not in seen]
    matches.sort(key=lambda m: m.id)
    if matches:
        # Only flip a "not_reviewed" event into "possible_duplicate" — never
        # downgrade an already-resolved "confirmed_duplicate"/"not_duplicate"
        # decision back to unresolved just because a later re-extraction
        # recomputed the same (correct, unchanged) fingerprint match. This
        # is what lets an admin's duplicate resolution survive a re-scrape.
        if event.duplicate_status == "not_reviewed":
            event.duplicate_status = "possible_duplicate"
        for match in matches:
            if match.duplicate_status == "not_reviewed":
                match.duplicate_status = "possible_duplicate"
    db.commit()
    return matches
