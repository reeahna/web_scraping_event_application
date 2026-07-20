import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy.orm import Session

from app.models.event import Event

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_text(value: str | None) -> str:
    return _WHITESPACE_RE.sub(" ", (value or "").strip()).casefold()


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


def update_fingerprint_and_duplicates(db: Session, event: Event) -> list[Event]:
    event.normalized_title = normalize_text(event.title)
    event.fingerprint = event_fingerprint(event)
    db.flush()
    matches = (
        db.query(Event)
        .filter(Event.id != event.id, Event.fingerprint == event.fingerprint)
        .order_by(Event.id)
        .all()
    )
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
