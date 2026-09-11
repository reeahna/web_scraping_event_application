"""Populate the category-photo placeholder pool from Unsplash.

Usage (from new_app/, with its venv active, and UNSPLASH_ACCESS_KEY set in the
environment or new_app/.env):

    python scripts/fetch_category_photos.py

Runs one Unsplash search per event category (well under the demo rate limit) and
replaces the stored pool. Re-run any time you want fresh photos. Requires only
the Unsplash Access Key (Client-ID); the Secret Key is not used.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings
from app.database import SessionLocal
from app.services.category_photos import fetch_and_store


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--per-category", type=int, default=10, help="photos to fetch per category (default 10)"
    )
    args = parser.parse_args()

    settings = get_settings()
    if not settings.unsplash_access_key:
        print(
            "UNSPLASH_ACCESS_KEY is not set. Add it to new_app/.env (local) or the "
            "service environment (Render), then re-run.",
            file=sys.stderr,
        )
        sys.exit(1)

    db = SessionLocal()
    try:
        stored = fetch_and_store(
            db,
            settings.unsplash_access_key,
            per_category=args.per_category,
            app_name=settings.app_name,
        )
    finally:
        db.close()

    total = sum(stored.values())
    for slug, count in stored.items():
        print(f"  {slug}: {count}")
    print(f"Stored {total} photos across {len(stored)} categories.")


if __name__ == "__main__":
    main()
