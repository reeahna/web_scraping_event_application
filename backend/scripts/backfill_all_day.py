"""Mark stored events as all-day where their extraction history says so.

A dry run by default: it prints how many events would change and why, and
changes nothing. See app/services/all_day_backfill.py for the rules.

Usage (from backend/, with its venv active):

    python scripts/backfill_all_day.py            # dry run, the impact report
    python scripts/backfill_all_day.py --apply    # set events.all_day
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import SessionLocal
from app.services.all_day_backfill import backfill_all_day


def _by_source(counter) -> str:
    return "".join(f"\n    {n:>6}  {source}" for source, n in counter.most_common()) or "\n    none"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="set all_day on the events found")
    parser.add_argument("--ids", action="store_true", help="also list the event ids")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        report = backfill_all_day(db, apply=args.apply)
    finally:
        db.close()

    verb = "Marked" if report.applied else "Would mark"
    print(
        f"{verb} {report.total_to_mark} event(s) all-day "
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
    if report.total_to_mark and not report.applied:
        print("Re-run with --apply to set them.")


if __name__ == "__main__":
    main()
