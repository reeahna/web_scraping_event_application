"""How fresh Bulletin's listings are, for visitors.

Two separate timestamps, never stand-ins for each other:

* ``Event.content_updated_at``: when visitor-facing details of one event last
  changed, either because a check found the source had changed them or because
  an administrator saved a correction. A check that finds nothing new leaves it
  alone. ``Event.scraped_at`` (last seen by a check) and ``Event.updated_at``
  (last database write) answer different questions.
* A town's "listings checked" time, read from extraction run records: the
  moment by which every source feeding the town's public listing had completed
  a successful check (see ``town_listings_checked``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.timezones import is_unset_zone, zone_info
from app.models.base import as_aware_utc
from app.models.extraction_run import ExtractionRun
from app.models.website import Website

# Run types that persist events (the scheduler and an admin's "Run import" both
# record "manual"; "scheduled" is accepted for older rows). Detection and
# preview runs never touch the public listing, so they never count as checks.
PERSISTENT_RUN_TYPES = ("manual", "scheduled")
# A partial run read the source but rejected some records as invalid; the
# listing was still checked, so it counts the same as a full success.
SUCCESSFUL_STATUSES = ("success", "partial")


# --- Event-level: what a visitor can see ----------------------------------


def visitor_snapshot(event: Any) -> tuple:
    """Everything about an event that a visitor sees, as a comparable value.

    Administrator corrections win where the page shows them (venue, address,
    category override), so a scraped change underneath a correction is not a
    change to what a visitor sees. Left out on purpose: the source link (URLs
    are rewritten for tracking without the event changing), coordinates (the
    venue and address are what the page shows), and lifecycle flags such as
    is_active, which decide whether the page is shown at all."""
    return (
        event.title,
        event.description,
        event.start_date,
        event.end_date,
        event.start_time,
        event.end_time,
        event.timezone,
        event.public_venue,
        event.public_address,
        event.image_url,
        event.category_override_id or event.category_id,
        bool(event.is_cancelled),
    )


def mark_if_changed(event: Any, before: tuple, *, now: datetime | None = None) -> bool:
    """Set ``content_updated_at`` when the event's visitor-facing details differ
    from the ``before`` snapshot. Returns whether they did."""
    if visitor_snapshot(event) == before:
        return False
    event.content_updated_at = now or datetime.now(UTC)
    return True


# --- Town-level: when the listing was last checked --------------------------


@dataclass(frozen=True)
class TownFreshness:
    """``checked_at``: every counted source completed a successful check at or
    after this instant (the oldest of their latest successes). ``sources`` is
    how many sources were counted, and ``failing`` how many of them failed
    their most recent check, so their listings date from ``checked_at`` or
    earlier rather than from their last attempt. ``pending`` counts public
    sources that have not completed a first successful check, which
    ``checked_at`` cannot speak for, so the page says so."""

    checked_at: datetime
    sources: int
    failing: int
    pending: int = 0


def _public_sources_query(db: Session, city_id: int):
    """The sources whose events can appear on the town's public pages: the
    same website conditions as the public listing (see
    app.repositories.public_events._visible). Disabled, archived, failing and
    not yet approved sources are out, because none of their events show."""
    return db.query(Website.id).filter(
        Website.city_id == city_id,
        Website.is_active.is_(True),
        Website.archived_at.is_(None),
        Website.active_configuration_version.isnot(None),
    )


def town_listings_checked(
    db: Session, city_id: int, *, now: datetime | None = None
) -> TownFreshness | None:
    """When the town's listings were last checked, or None when no public
    source has completed a successful check yet.

    Defined as the time by which every public source had completed a
    successful check: the oldest of each source's latest successful completed
    run. One source checked a minute ago and another that has failed since
    yesterday reads as yesterday, so a failing source never makes the whole
    town look current. A public source that has never completed a successful
    check (just approved) cannot be dated, so it is counted in ``pending``
    rather than silently left out. Only run records count: page views and
    failed or preview runs never move it."""
    source_ids = [row.id for row in _public_sources_query(db, city_id)]
    if not source_ids:
        return None
    finished = func.coalesce(ExtractionRun.completed_at, ExtractionRun.started_at)
    persistent = (
        ExtractionRun.website_id.in_(source_ids),
        ExtractionRun.run_type.in_(PERSISTENT_RUN_TYPES),
    )
    last_success = dict(
        db.query(ExtractionRun.website_id, func.max(finished))
        .filter(*persistent, ExtractionRun.status.in_(SUCCESSFUL_STATUSES))
        .group_by(ExtractionRun.website_id)
        .all()
    )
    if not last_success:
        return None
    last_failure = dict(
        db.query(ExtractionRun.website_id, func.max(finished))
        .filter(
            *persistent,
            ExtractionRun.website_id.in_(list(last_success)),
            ExtractionRun.status.notin_(SUCCESSFUL_STATUSES),
        )
        .group_by(ExtractionRun.website_id)
        .all()
    )
    successes = {key: as_aware_utc(value) for key, value in last_success.items()}
    failing = sum(
        1
        for key, failed_at in last_failure.items()
        if failed_at is not None and as_aware_utc(failed_at) > successes[key]
    )
    # A clock skew between the scheduler and the web process must never show a
    # check that has not happened yet.
    checked_at = min(min(successes.values()), now or datetime.now(UTC))
    return TownFreshness(
        checked_at=checked_at,
        sources=len(successes),
        failing=failing,
        pending=len(set(source_ids) - set(successes)),
    )


# --- Formatting --------------------------------------------------------------


def local_timestamp(value: datetime | None, zone_name: str | None) -> str:
    """ "November 19, 2026 at 8:45 PM EST" in the given zone, with the
    abbreviation in force at that instant (EST in winter, EDT in summer).
    Converted with the zone's own rules, never a fixed offset. A town still on
    the UTC default gets the time in UTC, labelled so."""
    if value is None:
        return ""
    moment = as_aware_utc(value)
    local = moment.astimezone(UTC if is_unset_zone(zone_name) else zone_info(zone_name))
    hour12 = local.hour % 12 or 12
    period = "AM" if local.hour < 12 else "PM"
    label = local.tzname() or "UTC"
    return (
        f"{local.strftime('%B')} {local.day}, {local.year} at "
        f"{hour12}:{local.minute:02d} {period} {label}"
    )


def event_zone_name(event: Any) -> str | None:
    """The zone an event page shows times in: the event's own, else its town's."""
    own = getattr(event, "timezone", None)
    if not is_unset_zone(own):
        return own
    city = getattr(event, "city", None)
    return city.timezone if city else own
