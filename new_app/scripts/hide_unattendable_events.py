"""Take down events already imported that the site should not show.

That is anything that is not something a college student would go to
(app/services/attendability.py) and anything outside the source's own town
(app/services/home_town.py). New imports leave these out on their own, and an
event that a source still lists is taken down the next time it is scraped.
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
from app.services.home_town import hide_out_of_town_events

OUT_OF_TOWN = "out_of_town"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="deactivate the events found")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        found = hide_unattendable_events(db, apply=args.apply)
        hidden = {event.id for event, _ in found}
        found += [
            (event, OUT_OF_TOWN)
            for event in hide_out_of_town_events(db, apply=args.apply)
            if event.id not in hidden
        ]
        for event, reason in found:
            where = f" ({event.public_address})" if reason == OUT_OF_TOWN else ""
            print(f"  [{reason}] #{event.id} {event.title}{where}")
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
