"""Report which sources leave events without a description.

Counts upcoming, active events per website and how many of them have no
description, worst sources first, with the extraction pattern each source uses.
Read-only.

Usage (from backend/, venv active; on Render the working dir is /srv/backend):

    python scripts/description_coverage.py            # sources missing any
    python scripts/description_coverage.py --all      # every source
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import case, func, or_  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.models.event import Event  # noqa: E402
from app.models.website import Website  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--all", action="store_true", help="include fully described sources")
    args = parser.parse_args()

    missing = or_(Event.description.is_(None), func.trim(Event.description) == "")
    with SessionLocal() as db:
        rows = (
            db.query(
                Website,
                func.count(Event.id),
                func.sum(case((missing, 1), else_=0)),
            )
            .join(Event, Event.website_id == Website.id)
            .filter(Event.is_active.is_(True), Event.start_date >= date.today())
            .group_by(Website.id)
            .all()
        )

    total = sum(count for _, count, _ in rows)
    total_missing = sum(int(m or 0) for _, _, m in rows)
    report = sorted(
        ((w, count, int(m or 0)) for w, count, m in rows),
        key=lambda r: (-r[2], r[0].name),
    )
    print(f"{'missing':>7} {'events':>6} {'pct':>5}  {'pattern':<24} source")
    for website, count, n_missing in report:
        if not n_missing and not args.all:
            continue
        pattern = (website.approved_pattern or {}).get("pattern_name", "-")
        pct = 100 * n_missing / count if count else 0
        print(f"{n_missing:>7} {count:>6} {pct:>4.0f}%  {pattern:<24} {website.name}")
    pct = 100 * total_missing / total if total else 0
    print(f"\n{total_missing} of {total} upcoming events ({pct:.0f}%) have no description.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
