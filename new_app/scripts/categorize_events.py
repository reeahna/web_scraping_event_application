"""Seed the starter categorization rules (only if none exist) and categorize
every existing event from the active rules.

Usage (from new_app/, with its venv active):

    python scripts/categorize_events.py

New events are categorized automatically as they are imported. Run this once to
create the starter rules and backfill events that were imported before the rules
existed, and again any time you change rules and want to re-run them over the
whole catalog. Editing or deleting rules in the admin Category Rules screen is
respected: the starter rules are created only when there are none.
"""

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import SessionLocal
from app.models.event import Event
from app.services.categorization import assign_category, seed_rules


def main() -> None:
    db = SessionLocal()
    try:
        created = seed_rules(db)
        print(f"Starter categorization rules created: {created}")

        events = db.query(Event).all()
        counts: Counter[str] = Counter()
        for index, event in enumerate(events, start=1):
            result = assign_category(db, event)
            if result and result.category:
                counts[result.category.slug] += 1
            if index % 500 == 0:
                db.commit()
                print(f"  ...{index}/{len(events)}")
        db.commit()

        print(f"Categorized {len(events)} events:")
        for slug, count in counts.most_common():
            print(f"  {slug}: {count}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
