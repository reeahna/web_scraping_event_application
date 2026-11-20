"""Recheck stored events against their source, one event at a time.

An import only corrects the events its listing still returns. A LiveWhale
feed lists upcoming events, so an event stored with a wrong day or time (the
UTC timestamp read as local time before the fix in #96), or with no venue
(before the feed was asked for locations), stays wrong once it leaves the
listing: Bulletin event 31925 showed a 7 PM Thursday recital at 12:00 AM on
Friday, with no venue, because nothing ever read it again.

This reads each suspect event back from the source by its own id
(`/live/json/events/id/<id>`, which still answers for past events), runs the
record through the same extractor and normalizer as an import, and corrects
the stored row in place:

* only rows it can match to exactly one source record and occurrence (same
  id, same session date for a session row, same title and page); anything
  else is listed for a person to look at, never guessed;
* only the date, time, timezone, venue and address change. Event ids, session
  ids, titles, categories, category overrides and an administrator's venue
  and address corrections (separate columns) are left as they are;
* a field the source leaves empty never clears a stored value, except an end
  the source explicitly says is absent, and an all-day event's times;
* nothing is deleted or created; a corrected row gets a provenance record
  tied to a "recheck" run, so its history shows what changed and from where;
* a row the source confirms (corrected, or already right) is recorded as
  checked: its "last seen by a check" time (`scraped_at`) moves forward and
  it gets a provenance record from the recheck run. Its `content_updated_at`
  moves only when a visitor-facing detail actually changed. A confirmed row
  is not selected again until its details change, so the next preview moves
  on to the events not yet confirmed. Rows that could not be read or matched
  are not marked and stay in the selection for a person to look at.

Dry run by default: `apply=False` reports the changes without writing.
"""

from __future__ import annotations

import dataclasses
import hashlib
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.timezones import local_date, resolve_zone_name
from app.extraction.locations import has_venue
from app.extraction.normalize import (
    ALL_DAY_MARKER,
    END_ABSENT_MARKER,
    normalize_candidate,
    plain_text,
)
from app.extraction.patterns import livewhale_json
from app.extraction.registry import REGISTRY
from app.extraction.types import EventCandidate, FetchRequest
from app.models.event import Event
from app.models.event_provenance import EventProvenance
from app.models.extraction_run import ExtractionRun
from app.models.website import Website
from app.repositories.event_provenance import create_event_provenance
from app.repositories.extraction_run import (
    create_extraction_run,
    get_latest_successful_run_for_website,
)
from app.services.event_freshness import mark_if_changed, visitor_snapshot
from app.services.fingerprints import update_fingerprint_and_duplicates

DEFAULT_LIMIT = 200
RECHECK_FIELDS = (
    "start_date", "start_time", "end_date", "end_time", "timezone", "venue", "address",
)


@dataclasses.dataclass(frozen=True)
class RecheckChange:
    event_id: int
    title: str
    changes: dict[str, tuple[Any, Any]]


@dataclasses.dataclass(frozen=True)
class RecheckReview:
    event_id: int
    title: str
    reason: str


@dataclasses.dataclass
class RecheckReport:
    supported: bool = True
    selected: int = 0
    checked: int = 0
    unchanged: int = 0
    over_limit: int = 0
    applied: bool = False
    run_id: int | None = None
    changes: list[RecheckChange] = dataclasses.field(default_factory=list)
    reviews: list[RecheckReview] = dataclasses.field(default_factory=list)


def supports_recheck(website: Website) -> bool:
    """Whether this source's events can be read back one by one (a LiveWhale
    feed). Never raises: an unreadable configuration just doesn't qualify."""
    try:
        config = _config(website)
    except Exception:  # noqa: BLE001 - shown on the admin page; must not break it
        return False
    return config is not None and config.pattern_name == livewhale_json.NAME and bool(
        config.api_endpoint
    )


def _config(website: Website):
    from app.services.extraction_runs import _resolve_configuration

    if not website.approved_pattern:
        return None
    return _resolve_configuration(website, use_approved=True)


def _zone_name(website: Website, config) -> str | None:
    from app.services.extraction_runs import _fallback_timezone

    return resolve_zone_name(config.timezone, _fallback_timezone(website))


def _confirmed_at(db: Session, website: Website) -> dict[int, datetime]:
    """When a recheck last confirmed each of this website's events against
    the source (latest provenance record of a "recheck" run)."""
    rows = (
        db.query(EventProvenance.event_id, EventProvenance.created_at)
        .join(ExtractionRun, ExtractionRun.id == EventProvenance.extraction_run_id)
        .filter(
            EventProvenance.website_id == website.id,
            ExtractionRun.run_type == "recheck",
        )
        .all()
    )
    confirmed: dict[int, datetime] = {}
    for event_id, created_at in rows:
        created_at = _aware(created_at)
        if created_at is not None and (
            event_id not in confirmed or created_at > confirmed[event_id]
        ):
            confirmed[event_id] = created_at
    return confirmed


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def suspect_events(
    db: Session, website: Website, *, zone_name: str | None, today: date | None = None
) -> list[Event]:
    """Events of this website worth reading back: current or upcoming (from
    yesterday, in the town's own calendar, so a day shifted forward by the
    old UTC reading is caught), from the source's own records, and either at
    a bare 12:00 AM, without a venue, or not refreshed by the latest import.
    Rows a recheck already confirmed are left out unless their details
    changed since (an import or an administrator's correction)."""
    since = (today or local_date(zone_name)) - timedelta(days=1)
    latest = get_latest_successful_run_for_website(db, website.id)
    stale = [Event.scraped_at < latest.started_at] if latest is not None else []
    rows = (
        db.query(Event)
        .filter(
            Event.website_id == website.id,
            Event.origin == "extracted",
            Event.archived_at.is_(None),
            Event.external_source_id.is_not(None),
            or_(Event.start_date >= since, Event.end_date >= since),
            or_(
                Event.start_time == time(0),
                Event.venue.is_(None),
                Event.venue == "",
                *stale,
                # Placeholders ("TBD") are matched in Python below.
                Event.venue.ilike("%tb%"),
                Event.venue.ilike("%n/a%"),
                Event.venue.ilike("none"),
            ),
        )
        .order_by(Event.start_date, Event.id)
        .all()
    )
    latest_start = _aware(latest.started_at) if latest is not None else None
    confirmed = _confirmed_at(db, website)

    def suspect(event: Event) -> bool:
        checked = confirmed.get(event.id)
        if checked is not None:
            changed = _aware(event.content_updated_at)
            if changed is None or changed <= checked:
                return False
        if event.start_time == time(0):
            return True
        if not has_venue({"venue": event.venue}) and not event.corrected_venue:
            return True
        scraped = _aware(event.scraped_at)
        return latest_start is not None and scraped is not None and scraped < latest_start

    return [event for event in rows if suspect(event)]


def _same_page(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    pa, pb = urlsplit(a), urlsplit(b)
    return (pa.netloc.lower(), pa.path.rstrip("/")) == (pb.netloc.lower(), pb.path.rstrip("/"))


def _same_title(a: str | None, b: str | None) -> bool:
    return bool(a and b) and (plain_text(a) or "").casefold() == (plain_text(b) or "").casefold()


def _match(event: Event, source_id: str, candidates: list[EventCandidate]) -> EventCandidate | str:
    """The one source record and occurrence this row is, or why not. Every
    identity the record carries must agree with the stored row: the LiveWhale
    id asked for, the title, the event's page and, for a session row, the
    series id and the session's own date. Anything ambiguous is left for a
    person."""
    stored_id = event.external_source_id or ""
    if not candidates:
        return "the source no longer returns this event"
    own = [c for c in candidates if str(c.raw.get("event_id") or "") == source_id]
    if not own:
        return "the source returned a different event id"
    same = [
        c for c in own
        if _same_title(c.title, event.title) and _same_page(c.canonical_url, event.canonical_url)
    ]
    if not same:
        return "the source's title or page for this id differs"
    if "@" in stored_id:
        sessions = [
            c for c in same
            if c.external_source_id == stored_id
            and c.recurrence_parent_id == source_id
            and (event.recurrence_parent_id in (None, source_id))
            and c.start_date is not None
            and c.start_date.isoformat() == stored_id.split("@", 1)[1]
        ]
        if len(sessions) != 1:
            return "the source did not return this session"
        return sessions[0]
    if len(same) != 1 or same[0].recurrence_parent_id or same[0].external_source_id != source_id:
        return "a repeating series stored as one row"
    return same[0]


def _proposed(event: Event, candidate: EventCandidate) -> dict[str, tuple[Any, Any]]:
    """Field changes the source record supports, using the same rules as an
    import (update_event) but limited to dates, times, timezone and place."""
    new: dict[str, Any] = {}
    if candidate.start_date is None:
        return {}
    all_day = ALL_DAY_MARKER in candidate.transformation_history
    new["start_date"] = candidate.start_date
    new["start_time"] = None if all_day else (candidate.start_time or event.start_time)
    if candidate.end_date is not None or END_ABSENT_MARKER in candidate.transformation_history:
        new["end_date"] = candidate.end_date
    if all_day or END_ABSENT_MARKER in candidate.transformation_history:
        new["end_time"] = None if all_day else candidate.end_time
    elif candidate.end_time is not None:
        new["end_time"] = candidate.end_time
    if candidate.timezone:
        new["timezone"] = candidate.timezone
    if candidate.venue is not None and has_venue({"venue": candidate.venue}):
        new["venue"] = candidate.venue
    if candidate.address is not None:
        new["address"] = candidate.address
    return {
        field: (getattr(event, field), value)
        for field, value in new.items()
        if getattr(event, field) != value
    }


async def recheck_website_events(
    db: Session,
    website: Website,
    *,
    apply: bool = False,
    limit: int = DEFAULT_LIMIT,
    triggered_by_user_id: int | None = None,
    today: date | None = None,
) -> RecheckReport:
    """Read suspect events back from the source and report (or, with
    `apply`, make) the corrections. At most `limit` source requests, one per
    distinct source id, through the import's own fetch layer."""
    from app.services import extraction_runs

    report = RecheckReport(applied=apply)
    config = _config(website)
    if config is None or not supports_recheck(website):
        report.supported = False
        return report
    zone_name = _zone_name(website, config)
    if zone_name and zone_name != config.timezone:
        config = config.model_copy(update={"timezone": zone_name})
    pattern = livewhale_json.LiveWhalePattern()
    # The single-record endpoint is plain JSON: always the ordinary HTTP
    # client (SSRF-checked), never a browser, whatever the source's listing
    # uses, so a recheck can't start Chromium on a small instance.
    fetch = extraction_runs.HttpFetchStrategy()

    events = suspect_events(db, website, zone_name=zone_name, today=today)
    report.selected = len(events)
    by_source: dict[str, list[Event]] = {}
    for event in events:
        by_source.setdefault((event.external_source_id or "").split("@", 1)[0], []).append(event)

    started_at = datetime.now(UTC)
    fetched: dict[str, tuple[list[EventCandidate], str, str] | None] = {}
    for source_id in by_source:
        if len(fetched) >= limit:
            report.over_limit += len(by_source[source_id])
            continue
        url = livewhale_json.record_url(config.api_endpoint or "", source_id)
        if url is None:
            fetched[source_id] = None
            continue
        response = await fetch.fetch(FetchRequest(url=url), config.fetch)
        if response.blocked_reason is not None or response.status_code != 200:
            fetched[source_id] = None
            continue
        candidates = pattern.finalize_candidates(pattern.extract(response, config), config)
        normalized = [
            normalize_candidate(c, config, fallback_timezone=zone_name) for c in candidates
        ]
        body_hash = hashlib.sha256(response.text.encode("utf-8")).hexdigest()
        fetched[source_id] = (normalized, response.final_url, body_hash)

    # Every row the source confirmed, corrected or not.
    matched: list[tuple[Event, EventCandidate, dict[str, tuple[Any, Any]], str]] = []
    for source_id, rows in by_source.items():
        if source_id not in fetched:
            continue
        result = fetched[source_id]
        for event in rows:
            if result is None:
                report.reviews.append(
                    RecheckReview(event.id, event.title, "the source could not be read")
                )
                continue
            report.checked += 1
            candidate = _match(event, source_id, result[0])
            if isinstance(candidate, str):
                report.reviews.append(RecheckReview(event.id, event.title, candidate))
                continue
            changes = _proposed(event, candidate)
            if changes:
                report.changes.append(RecheckChange(event.id, event.title, changes))
            else:
                report.unchanged += 1
            matched.append((event, candidate, changes, result[2]))

    if not apply or not matched:
        return report

    run = create_extraction_run(
        db,
        website_id=website.id,
        configuration_version=website.active_configuration_version,
        pattern_name=config.pattern_name,
        run_type="recheck",
        status="success",
        source_url=config.api_endpoint or "",
        final_url=None,
        events_found=report.checked,
        events_valid=report.checked,
        events_updated=len(report.changes),
        warnings=[f"recheck_needs_review:{len(report.reviews)}"] if report.reviews else [],
        started_at=started_at,
        completed_at=datetime.now(UTC),
        initiating_user_id=triggered_by_user_id,
    )
    version = REGISTRY.get(config.pattern_name).version
    checked_at = datetime.now(UTC)
    for event, candidate, changes, body_hash in matched:
        # Seen by a check either way; content_updated_at only on a real change.
        event.scraped_at = checked_at
        if changes:
            before = visitor_snapshot(event)
            for field, (_, value) in changes.items():
                setattr(event, field, value)
            mark_if_changed(event, before, now=checked_at)
            update_fingerprint_and_duplicates(db, event, commit=False)
        create_event_provenance(
            db,
            event_id=event.id,
            extraction_run_id=run.id,
            website_id=website.id,
            source_page=candidate.source_page,
            extraction_pattern=candidate.extraction_pattern,
            pattern_version=version,
            raw_record_hash=candidate.raw_record_hash,
            source_response_hash=body_hash,
            field_source_paths={
                field: path
                for field, path in candidate.field_source_paths.items()
                if field in {"start_datetime", "end_datetime", "venue", "address", "timezone"}
            },
            transformation_history=[
                *candidate.transformation_history,
                *((f"recheck:{field}" for field in changes) if changes else ["recheck:confirmed"]),
            ],
            commit=False,
        )
    db.commit()
    report.run_id = run.id
    return report
