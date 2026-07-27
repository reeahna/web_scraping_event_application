"""Legacy vs new-engine comparison (Phase 16).

Reads the legacy application's events database **strictly read-only** (SQLite
opened with `mode=ro`) and compares a legacy source's events against the new
engine's preview candidates. It never writes to the legacy database, never runs
a legacy scraper, and never starts a scheduler — the legacy side is just data.

The comparison itself is pure and pattern-independent: it takes normalized
`ComparableEvent`s from either side and reports matched, legacy-only, new-only,
per-field differences, likely duplicates, and validation differences. A legacy
source that cannot be read is reported as *unavailable* rather than failing.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

_WS = re.compile(r"\s+")

# Fields compared for a matched pair, in report order.
COMPARED_FIELDS = (
    "title", "start_date", "end_date", "start_time",
    "venue", "address", "url", "category", "image_url", "recurrence",
)


class LegacyUnavailable(Exception):
    """The legacy database/source could not be read — recorded, never fatal."""


@dataclass(frozen=True)
class ComparableEvent:
    title: str | None
    start_date: str | None  # ISO date string
    end_date: str | None
    start_time: str | None
    venue: str | None
    address: str | None
    url: str | None
    category: str | None
    image_url: str | None
    has_recurrence: bool = False
    valid: bool = True


@dataclass
class MatchedPair:
    key: tuple
    legacy: ComparableEvent
    new: ComparableEvent
    field_differences: list[str] = field(default_factory=list)


@dataclass
class ComparisonReport:
    legacy_source: str
    legacy_available: bool
    legacy_count: int = 0
    new_count: int = 0
    matched: list[MatchedPair] = field(default_factory=list)
    legacy_only: list[ComparableEvent] = field(default_factory=list)
    new_only: list[ComparableEvent] = field(default_factory=list)
    likely_duplicate_keys: list[str] = field(default_factory=list)
    new_invalid: list[ComparableEvent] = field(default_factory=list)

    @property
    def matched_count(self) -> int:
        return len(self.matched)

    @property
    def field_difference_count(self) -> int:
        return sum(1 for m in self.matched if m.field_differences)

    def as_dict(self) -> dict:
        return {
            "legacy_source": self.legacy_source,
            "legacy_available": self.legacy_available,
            "legacy_count": self.legacy_count,
            "new_count": self.new_count,
            "matched": self.matched_count,
            "with_field_differences": self.field_difference_count,
            "legacy_only": len(self.legacy_only),
            "new_only": len(self.new_only),
            "likely_duplicates": len(self.likely_duplicate_keys),
            "new_invalid": len(self.new_invalid),
        }


def _norm(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = _WS.sub(" ", str(value)).strip().lower()
    return cleaned or None


def _match_key(event: ComparableEvent) -> tuple:
    # Title + start date is the identity for comparison; a URL alone is not
    # reliable across engines that rewrite/normalize URLs differently.
    return (_norm(event.title), event.start_date)


def _field_differences(legacy: ComparableEvent, new: ComparableEvent) -> list[str]:
    diffs: list[str] = []
    for f in COMPARED_FIELDS:
        if f == "recurrence":
            if legacy.has_recurrence != new.has_recurrence:
                diffs.append(f)
            continue
        lval, nval = getattr(legacy, f), getattr(new, f)
        if f in ("title", "venue", "address", "category"):
            lval, nval = _norm(lval), _norm(nval)
        if (lval or None) != (nval or None):
            diffs.append(f)
    return diffs


def compare_events(
    legacy: list[ComparableEvent], new: list[ComparableEvent], *, legacy_source: str,
    legacy_available: bool = True,
) -> ComparisonReport:
    report = ComparisonReport(
        legacy_source=legacy_source, legacy_available=legacy_available,
        legacy_count=len(legacy), new_count=len(new),
    )

    legacy_by_key: dict[tuple, list[ComparableEvent]] = {}
    new_by_key: dict[tuple, list[ComparableEvent]] = {}
    for e in legacy:
        legacy_by_key.setdefault(_match_key(e), []).append(e)
    for e in new:
        new_by_key.setdefault(_match_key(e), []).append(e)

    # Likely duplicates: the same key appearing more than once on either side.
    dup_keys = {
        _key_str(k) for k, items in legacy_by_key.items() if len(items) > 1
    } | {
        _key_str(k) for k, items in new_by_key.items() if len(items) > 1
    }
    report.likely_duplicate_keys = sorted(dup_keys)

    for key, legacy_items in legacy_by_key.items():
        if key in new_by_key:
            pair = MatchedPair(key=key, legacy=legacy_items[0], new=new_by_key[key][0])
            pair.field_differences = _field_differences(pair.legacy, pair.new)
            report.matched.append(pair)
        else:
            report.legacy_only.extend(legacy_items)

    for key, new_items in new_by_key.items():
        if key not in legacy_by_key:
            report.new_only.extend(new_items)

    report.new_invalid = [e for e in new if not e.valid]
    return report


def _key_str(key: tuple) -> str:
    title, start = key
    return f"{title or '?'} @ {start or '?'}"


# --- legacy read-only reader -------------------------------------------------


def default_legacy_db_path() -> Path:
    # legacy_app sits alongside new_app in the project root.
    return Path(__file__).resolve().parents[3] / "legacy_app" / "events.db"


def read_legacy_events(
    source: str, *, db_path: Path | None = None
) -> list[ComparableEvent]:
    """Read one legacy source's events, read-only. Raises LegacyUnavailable if
    the database is missing/unreadable so the caller can record 'unavailable'."""
    path = db_path or default_legacy_db_path()
    if not path.exists():
        raise LegacyUnavailable(f"legacy database not found at {path}")
    try:
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error as exc:  # pragma: no cover - environment dependent
        raise LegacyUnavailable(str(exc)) from exc
    try:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT title, date, end_date, time, venue, address, url, category, image_url "
            "FROM events WHERE source = ?",
            (source,),
        ).fetchall()
    except sqlite3.Error as exc:
        raise LegacyUnavailable(str(exc)) from exc
    finally:
        con.close()
    return [
        ComparableEvent(
            title=r["title"], start_date=_as_iso(r["date"]), end_date=_as_iso(r["end_date"]),
            start_time=r["time"], venue=r["venue"], address=r["address"], url=r["url"],
            category=r["category"], image_url=r["image_url"], has_recurrence=False, valid=True,
        )
        for r in rows
    ]


def _as_iso(value) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    return text[:10] if text else None


MIGRATION_STATUSES = ("pending", "migrated", "unavailable")


def set_migration_status(db, website, status: str, *, legacy_source: str | None = None,
                         now: datetime | None = None) -> None:
    """Record a source's migration status after review. Never touches live
    extraction. `migrated` stamps the time; `unavailable` marks a legacy source
    that can no longer be read (so the phase is never blocked by it)."""
    if status not in MIGRATION_STATUSES:
        raise ValueError(f"unknown migration status: {status}")
    website.legacy_migration_status = status
    if legacy_source is not None:
        website.legacy_source_name = legacy_source
    website.legacy_migrated_at = (now or datetime.now(UTC)) if status == "migrated" else None
    db.commit()


async def run_comparison(db, website, legacy_source: str) -> ComparisonReport:
    """Compare a website's new-engine preview against its legacy events. Reads
    the legacy DB read-only and runs the new preview — never a legacy scraper,
    never a scheduler. An unreadable legacy source yields an 'unavailable'
    report and status rather than an error."""
    from app.services.extraction_runs import preview_extraction_detailed

    try:
        legacy = read_legacy_events(legacy_source)
        available = True
    except LegacyUnavailable:
        legacy, available = [], False

    outcome = await preview_extraction_detailed(db, website)
    new = [candidate_to_comparable(c, valid=r.is_valid) for c, r in outcome.outcomes]
    report = compare_events(
        legacy, new, legacy_source=legacy_source, legacy_available=available
    )
    if not available:
        set_migration_status(db, website, "unavailable", legacy_source=legacy_source)
    return report


def candidate_to_comparable(candidate, *, valid: bool) -> ComparableEvent:
    """Map a new-engine EventCandidate to the comparison shape."""
    def iso(d: date | None) -> str | None:
        return d.isoformat() if isinstance(d, date) else None

    return ComparableEvent(
        title=candidate.title,
        start_date=iso(candidate.start_date),
        end_date=iso(candidate.end_date),
        start_time=candidate.start_time.isoformat() if candidate.start_time else None,
        venue=candidate.venue,
        address=candidate.address,
        url=candidate.canonical_url,
        category=candidate.source_category,
        image_url=candidate.image_url,
        has_recurrence=bool(getattr(candidate, "recurrence_parent_id", None)),
        valid=valid,
    )
