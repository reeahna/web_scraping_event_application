"""Take down events already imported that are not something a person can go to.

New imports leave these out on their own (app/services/attendability.py), and
an event that a source still lists is taken down the next time it is scraped.
This clears the rest in one pass. It lists what it would hide and changes
nothing unless --apply is given.

Usage (from new_app/, with its venv active):

    python scripts/hide_unattendable_events.py           # dry run
    python scripts/hide_unattendable_events.py --apply   # deactivate them
"""

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import SessionLocal
from app.services.attendability import hide_unattendable_events


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="deactivate the events found")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        found = hide_unattendable_events(db, apply=args.apply)
        for event, reason in found:
            print(f"  [{reason}] #{event.id} {event.title}")
    finally:
        db.close()

    by_reason = Counter(reason for _, reason in found)
    summary = ", ".join(f"{n} {reason}" for reason, n in sorted(by_reason.items())) or "none"
    verb = "Deactivated" if args.apply else "Would deactivate"
    print(f"{verb} {len(found)} event(s): {summary}.")
    if found and not args.apply:
        print("Re-run with --apply to deactivate them.")


if __name__ == "__main__":
    main()
