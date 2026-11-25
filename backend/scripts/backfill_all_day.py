"""Mark stored events as all-day where their extraction history says so.

A dry run by default: it prints how many events would change and why, and
changes nothing. See app/services/all_day_backfill.py for the rules.

This script never writes. Marking events is done only on the admin page
(/admin/events/all-day-backfill): a Super Administrator checks a reviewed list
of event ids, and Apply then uses exactly that saved check. Here you can check
a list the same way, which changes nothing.

Usage (from backend/, with its venv active):

    python scripts/backfill_all_day.py --ids      # dry run, the impact report + ids
    python scripts/backfill_all_day.py --ids-file reviewed.txt \
        --reviewed-at 2026-11-24T23:20:00+00:00   # check the list, change nothing
"""

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import SessionLocal
from app.services.all_day_backfill import (
    apply_all_day_to_ids,
    backfill_all_day,
    parse_event_ids,
)


def _by_source(counter) -> str:
    return "".join(f"\n    {n:>6}  {source}" for source, n in counter.most_common()) or "\n    none"


def _check_ids(args) -> None:
    reviewed_at = datetime.fromisoformat(args.reviewed_at)
    if reviewed_at.tzinfo is None:
        reviewed_at = reviewed_at.replace(tzinfo=UTC)
    with open(args.ids_file, encoding="utf-8") as handle:
        ids = parse_event_ids(handle.read())
    db = SessionLocal()
    try:
        report = apply_all_day_to_ids(db, ids, reviewed_at=reviewed_at, apply=False)
    finally:
        db.close()
    print(f"Would mark {len(report.changed_ids)} of {report.requested} listed event(s) all-day.")
    if report.changed_ids:
        print("    ids: " + ", ".join(map(str, report.changed_ids)))
    print(f"Skipped: {len(report.skipped)}.")
    for event_id, reason in report.skipped:
        print(f"    {event_id}: {reason}")
    if report.changed_ids:
        print("Nothing changed. To apply, check the list on the admin page, then Apply.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ids", action="store_true", help="also list the event ids")
    parser.add_argument("--ids-file", help="reviewed event ids to check (changes nothing)")
    parser.add_argument(
        "--reviewed-at", help="when the list was reviewed (ISO 8601); later edits are skipped"
    )
    args = parser.parse_args()

    if args.ids_file:
        if not args.reviewed_at:
            parser.error("--ids-file needs --reviewed-at")
        _check_ids(args)
        return

    db = SessionLocal()
    try:
        report = backfill_all_day(db, apply=False)
    finally:
        db.close()

    print(
        f"Would mark {report.total_to_mark} event(s) all-day "
        "(date-only, and the latest extraction says the source flags it all-day):"
        + _by_source(report.to_mark)
    )
    if args.ids and report.marked_ids:
        print("    ids: " + ", ".join(map(str, report.marked_ids)))
    print(f"Already all-day: {report.already_all_day}.")
    print(
        f"Left as Time TBD: {sum(report.date_only_unmarked.values())} date-only event(s) "
        "with no all-day signal in their history:" + _by_source(report.date_only_unmarked)
    )
    print(
        f"Left as Time TBD: {sum(report.time_tbd_sources.values())} event(s) from sources set "
        'to "time not announced":' + _by_source(report.time_tbd_sources)
    )
    print(
        f"Left for the next import: {sum(report.full_day_span.values())} event(s) stored as "
        "12:00 AM to 11:59 PM; the source's own wording decides on re-import:"
        + _by_source(report.full_day_span)
    )
    if args.ids and report.full_day_span_ids:
        print("    ids: " + ", ".join(map(str, report.full_day_span_ids)))
    if report.marker_with_time:
        print(
            f"Left unchanged: {len(report.marker_with_time)} event(s) flagged all-day in "
            "history but holding a clock time: " + ", ".join(map(str, report.marker_with_time))
        )
    if report.total_to_mark:
        print("To apply, review the ids, then check and apply them on the admin page.")


if __name__ == "__main__":
    main()
