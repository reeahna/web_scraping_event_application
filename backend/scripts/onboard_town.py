"""Find a college town's event sources with Claude, from the command line.

The same search the admin page "Onboard a town" runs in the scheduler, for a
trial run or for writing the daily source CSV by hand. Needs ANTHROPIC_API_KEY
(in .env or the environment).

Usage (from backend/, with its venv active):

    python scripts/onboard_town.py "Ann Arbor" --state Michigan
    python scripts/onboard_town.py "Ann Arbor" --state MI \\
        --university "University of Michigan" --csv ann-arbor-mi.csv
    python scripts/onboard_town.py "Ann Arbor" --state MI --apply   # create city + queue

Without --apply nothing is written to the database: the CSV (url, name,
source_display_name, city_slug, timezone) is printed or saved, ready for the
Onboarding page. With --apply the city is created when missing and the sources
are queued for onboarding; the scheduler processes them.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.town_onboarding import (  # noqa: E402
    TownOnboardingError,
    apply_proposal,
    discover_town,
    proposal_csv,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("town", help="The town, e.g. 'Ann Arbor'")
    parser.add_argument("--state", default=None, help="State name or code")
    parser.add_argument(
        "--university", action="append", default=[], help="A university in town (repeatable)"
    )
    parser.add_argument("--csv", type=Path, default=None, help="Write the CSV here")
    parser.add_argument(
        "--apply", action="store_true", help="Create the city and queue the sources"
    )
    args = parser.parse_args()

    print(f"Asking Claude for {args.town}'s event sources; this takes a few minutes...")
    try:
        proposal = discover_town(args.town, args.state, args.university)
    except TownOnboardingError as exc:
        print(f"Failed: {exc}", file=sys.stderr)
        return 1

    print(
        f"{proposal.city_name}, {proposal.state_or_region} ({proposal.slug}, "
        f"{proposal.timezone}): {len(proposal.sources)} sources, "
        f"{proposal.count('campus')} campus and {proposal.count('town')} town."
    )
    for item in proposal.rejected:
        print(f"  left out {item['url']}: {item['reason']}")

    text = proposal_csv(proposal)
    if args.csv:
        args.csv.write_text(text, encoding="utf-8")
        print(f"Wrote {args.csv}")
    else:
        print(text)

    if args.apply:
        from app.database import SessionLocal

        db = SessionLocal()
        try:
            city, batch = apply_proposal(db, proposal, requested_by_user_id=None)
            print(f"City {city.slug} ready; onboarding batch {batch.id} queued.")
        finally:
            db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
