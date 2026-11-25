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
from datetime import UTC, datetime, timedelta
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


# --- Source health, as visitors see it --------------------------------------

# Events from a source that is failing or overdue are labelled "May be out of
# date" once the source has gone this long without a successful check.
STALE_AFTER = timedelta(hours=24)


@dataclass(frozen=True)
class SourceHealth:
    """One public source's health, from its run records and lifecycle state.

    Health never decides whether the source's events are public (see
    app.repositories.public_events.public_source_conditions); it decides what
    visitors are told about how current they are."""

    website_id: int
    name: str
    last_success_at: datetime | None
    # Three failures in a row moved it to FAILING (imports paused), or its
    # latest check failed.
    failing: bool
    # Automatic imports were turned off on purpose: updated by hand, so it is
    # neither expected on a schedule nor allowed to date the whole town.
    paused: bool
    # Past its expected interval plus the grace period without a success.
    overdue: bool
    # Failing or overdue, and no successful check for STALE_AFTER: its events
    # are labelled as possibly out of date.
    stale: bool


def _source_name(website: Website) -> str:
    return website.source_display_name or website.name


def source_health(
    db: Session, websites: list[Website], *, now: datetime | None = None
) -> dict[int, SourceHealth]:
    """Health for each of ``websites``, keyed by id. Two grouped queries."""
    if not websites:
        return {}
    from app.core.onboarding import ACTIVE, FAILING
    from app.services.source_freshness import TARGET_OFF, _target

    now = now or datetime.now(UTC)
    ids = [site.id for site in websites]
    finished = func.coalesce(ExtractionRun.completed_at, ExtractionRun.started_at)
    persistent = (
        ExtractionRun.website_id.in_(ids),
        ExtractionRun.run_type.in_(PERSISTENT_RUN_TYPES),
    )
    last_success = {
        key: as_aware_utc(value)
        for key, value in db.query(ExtractionRun.website_id, func.max(finished))
        .filter(*persistent, ExtractionRun.status.in_(SUCCESSFUL_STATUSES))
        .group_by(ExtractionRun.website_id)
    }
    last_failure = {
        key: as_aware_utc(value)
        for key, value in db.query(ExtractionRun.website_id, func.max(finished))
        .filter(*persistent, ExtractionRun.status.notin_(SUCCESSFUL_STATUSES))
        .group_by(ExtractionRun.website_id)
        if value is not None
    }
    result: dict[int, SourceHealth] = {}
    for site in websites:
        success_at = last_success.get(site.id)
        failure_at = last_failure.get(site.id)
        failing = site.onboarding_status == FAILING or (
            failure_at is not None and (success_at is None or failure_at > success_at)
        )
        paused = (
            site.onboarding_status == ACTIVE
            and _target(site, should_be_checked=True)[0] == TARGET_OFF
        )
        fresh_since = success_at or (
            as_aware_utc(site.approved_at) if site.approved_at else None
        )
        overdue = not paused and _is_overdue(site, fresh_since, now)
        stale = (
            not paused
            and (failing or overdue)
            and (fresh_since is None or now - fresh_since > STALE_AFTER)
        )
        result[site.id] = SourceHealth(
            website_id=site.id,
            name=_source_name(site),
            last_success_at=success_at,
            failing=failing,
            paused=paused,
            overdue=overdue,
            stale=stale,
        )
    return result


def stale_source_ids(
    db: Session, website_ids, *, now: datetime | None = None
) -> set[int]:
    """Which of these sources' events should be labelled "May be out of date"."""
    ids = {website_id for website_id in website_ids if website_id is not None}
    if not ids:
        return set()
    sites = list(db.query(Website).filter(Website.id.in_(ids)))
    return {key for key, health in source_health(db, sites, now=now).items() if health.stale}


# --- Town-level: when the listing was last checked --------------------------


@dataclass(frozen=True)
class TownFreshness:
    """``checked_at``: every counted source completed a successful check at or
    after this instant (the oldest of their latest successes). ``sources`` is
    how many sources were counted, and ``failing`` how many of them are
    failing (their latest check failed, or imports are paused after repeated
    failures), so their listings date from ``checked_at`` or earlier rather
    than from their last attempt. ``pending`` counts expected sources that
    have not completed a first successful check, which ``checked_at`` cannot
    speak for, so the page says so. ``paused`` counts public sources whose
    automatic imports are off on purpose: they are left out of
    ``checked_at`` so a source nobody runs cannot pin the town to an old
    date, and the page says they were left out."""

    checked_at: datetime
    sources: int
    failing: int
    pending: int = 0
    # The source whose last successful check is ``checked_at``: the one that
    # dates the whole town.
    oldest_website_id: int | None = None
    oldest_source_name: str = ""
    # When every *other* counted source had last been checked; None with a
    # single source.
    others_checked_at: datetime | None = None
    # Why the oldest source is holding the town back, when it is: "failing"
    # (its latest check failed, or its imports are paused after repeated
    # failures) or "overdue" (no successful check for more than its expected
    # interval plus the grace period). Empty when it is simply the oldest of
    # a set of up-to-date sources. Set for a town's only source too, so a
    # single failing source is never presented as current.
    held_back_by: str = ""
    # Counted sources other than the oldest that are overdue.
    overdue: int = 0
    paused: int = 0

    @property
    def held_back(self) -> bool:
        return bool(self.held_back_by)


def _public_sources(db: Session, city_id: int) -> list[Website]:
    """The sources whose events can appear on the town's public pages: the
    same website conditions as the public listing. Failing sources are in
    (their events stay public); disabled, archived and not yet approved
    sources are out, because none of their events show."""
    from app.repositories.public_events import public_source_conditions

    return list(
        db.query(Website).filter(Website.city_id == city_id, *public_source_conditions(Website))
    )


def town_listings_checked(
    db: Session, city_id: int, *, now: datetime | None = None
) -> TownFreshness | None:
    """When the town's listings were last checked, or None when no counted
    source has completed a successful check yet.

    Defined as the time by which every expected source had completed a
    successful check: the oldest of each source's latest successful completed
    run. One source checked a minute ago and another that has failed since
    yesterday reads as yesterday, so a failing source never makes the whole
    town look current. Expected sources are the public ones on automatic
    imports, failing ones included; sources whose automatic imports are off
    on purpose are left out (and counted in ``paused``) unless they are all
    the town has. An expected source that has never completed a successful
    check (just approved) cannot be dated, so it is counted in ``pending``
    rather than silently left out. Only run records count: page views and
    failed or preview runs never move it."""
    sites = _public_sources(db, city_id)
    if not sites:
        return None
    now = now or datetime.now(UTC)
    health = source_health(db, sites, now=now)
    expected = [site for site in sites if not health[site.id].paused]
    counted = expected or sites
    paused = len(sites) - len(expected) if expected else 0
    successes = {
        site.id: health[site.id].last_success_at
        for site in counted
        if health[site.id].last_success_at is not None
    }
    if not successes:
        return None
    failing_ids = {key for key in successes if health[key].failing}
    # A clock skew between the scheduler and the web process must never show a
    # check that has not happened yet.
    oldest_id = min(successes, key=lambda key: (successes[key], key))
    checked_at = min(successes[oldest_id], now)
    others = [at for key, at in successes.items() if key != oldest_id]
    held_back_by = ""
    if oldest_id in failing_ids:
        held_back_by = "failing"
    elif health[oldest_id].overdue:
        held_back_by = "overdue"
    return TownFreshness(
        checked_at=checked_at,
        sources=len(successes),
        failing=len(failing_ids),
        pending=len(counted) - len(successes),
        oldest_website_id=oldest_id,
        oldest_source_name=health[oldest_id].name,
        others_checked_at=min(min(others), now) if others else None,
        held_back_by=held_back_by,
        overdue=sum(
            1
            for key in successes
            if key != oldest_id and health[key].overdue and key not in failing_ids
        ),
        paused=paused,
    )


def _is_overdue(website: Website | None, last_success: datetime | None, now: datetime) -> bool:
    """Past its expected interval plus the grace period (see
    app.services.source_freshness), so it is not just the next one due."""
    if website is None or last_success is None:
        return False
    from app.services.source_freshness import OVERDUE_GRACE, _target

    _, interval = _target(website, should_be_checked=True)
    return now - last_success > timedelta(minutes=interval) + OVERDUE_GRACE


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
