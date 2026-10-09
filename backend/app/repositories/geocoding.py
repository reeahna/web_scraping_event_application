"""Read-only geocoding progress for the admin status page."""

from __future__ import annotations

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.models.city import City
from app.models.event import Event
from app.repositories.public_events import current_public_date

# What a skip reason means, in words an admin reading the status page can use.
SKIP_REASONS = {
    "source_coordinates": "already_had_coordinates",
    "protected_override": "admin_set_coordinates",
    "no_address": "no_location",
}


def geocoding_overview(db: Session) -> dict:
    """`counts` covers every event; `upcoming` and `upcoming_by_city` only the
    upcoming events the public map can show; `unmatched_upcoming` lists the
    most common locations that found no match, which an admin can fix by
    correcting the event's venue or address."""
    today = current_public_date()
    upcoming = or_(
        and_(Event.end_date.isnot(None), Event.end_date >= today),
        and_(Event.end_date.is_(None), Event.start_date.isnot(None), Event.start_date >= today),
    )
    rows = db.execute(
        select(Event.geocode_status, func.count()).group_by(Event.geocode_status)
    ).all()
    skipped = db.execute(
        select(Event.geocode_last_error, func.count())
        .where(Event.geocode_status == "skipped")
        .group_by(Event.geocode_last_error)
    ).all()
    upcoming_rows = db.execute(
        select(Event.geocode_status, func.count())
        .where(upcoming)
        .group_by(Event.geocode_status)
    ).all()
    city_rows = db.execute(
        select(City.name, Event.geocode_status, func.count())
        .join(City, Event.city_id == City.id)
        .where(upcoming)
        .group_by(City.name, Event.geocode_status)
        .order_by(City.name)
    ).all()
    unmatched = db.execute(
        select(City.name, Event.venue, Event.address, func.count().label("n"))
        .join(City, Event.city_id == City.id, isouter=True)
        .where(upcoming, Event.geocode_status == "needs_review")
        .group_by(City.name, Event.venue, Event.address)
        .order_by(func.count().desc())
        .limit(50)
    ).all()

    by_city: dict[str, dict[str, int]] = {}
    for city_name, status_value, count in city_rows:
        by_city.setdefault(city_name, {})[status_value] = count
    return {
        "counts": {status_value: count for status_value, count in rows},
        "skipped_reasons": {
            SKIP_REASONS.get(reason or "", reason or "unknown"): count
            for reason, count in skipped
        },
        "upcoming": {status_value: count for status_value, count in upcoming_rows},
        "upcoming_by_city": by_city,
        "unmatched_upcoming": [
            {"city": city_name, "venue": venue, "address": address, "events": n}
            for city_name, venue, address, n in unmatched
        ],
    }
