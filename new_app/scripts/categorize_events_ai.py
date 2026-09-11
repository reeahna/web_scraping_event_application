"""Categorize events with Gemini (Google AI Studio).

A one-shot, resumable labeling pass that reads each event's own words and
assigns the best-fitting category — far more accurately than the keyword rules,
which mislabel things like a "cooking class" as education instead of food. It
writes the automatic category only (category_source="ai") and never touches an
administrator's manual override.

Usage (from new_app/, with its venv active and GEMINI_API_KEY set in .env):

    python scripts/categorize_events_ai.py            # label every non-AI event
    python scripts/categorize_events_ai.py --only-other   # only the "Other" pile
    python scripts/categorize_events_ai.py --force        # re-label everything
    python scripts/categorize_events_ai.py --limit 50     # small trial run

By default events already labeled by a previous AI run are skipped, so the pass
is idempotent and cheap to resume: re-running only spends API calls on events it
has not labeled yet. Get a free key at https://aistudio.google.com/apikey and
put it in .env as GEMINI_API_KEY (locally) or in the Render environment.
"""

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings
from app.database import SessionLocal
from app.models.event import Event
from app.models.event_category import EventCategory
from app.services.ai_categorizer import (
    CategoryOption,
    EventToClassify,
    GeminiCategorizer,
    GeminiError,
    GeminiQuotaExceeded,
)
from app.services.categorization import set_ai_category


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Categorize events with Gemini.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-label every event, including ones a previous AI run already labeled.",
    )
    parser.add_argument(
        "--only-other",
        action="store_true",
        help="Only label events currently uncategorized or in the 'Other' category.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Stop after this many events (for a small trial run).",
    )
    return parser.parse_args()


def _select_events(db, *, force: bool, only_other: bool, limit: int | None) -> list[Event]:
    query = db.query(Event)
    if not force:
        query = query.filter(Event.category_source != "ai")
    if only_other:
        other = (
            db.query(EventCategory.id).filter(EventCategory.slug == "other").scalar_subquery()
        )
        query = query.filter(
            (Event.category_id.is_(None)) | (Event.category_id == other)
        )
    query = query.order_by(Event.id)
    if limit is not None:
        query = query.limit(limit)
    return query.all()


def main() -> None:
    args = _parse_args()
    settings = get_settings()
    if not settings.gemini_api_key:
        print(
            "No GEMINI_API_KEY configured. Get a free key at "
            "https://aistudio.google.com/apikey and add it to .env as "
            "GEMINI_API_KEY, then re-run.",
            file=sys.stderr,
        )
        sys.exit(1)

    db = SessionLocal()
    try:
        categories = (
            db.query(EventCategory).filter(EventCategory.is_active.is_(True)).all()
        )
        by_slug = {category.slug: category for category in categories}
        options = [
            CategoryOption(slug=c.slug, name=c.name, description=c.description)
            for c in categories
        ]
        if not options:
            print("No active event categories exist; seed the taxonomy first.", file=sys.stderr)
            sys.exit(1)

        events = _select_events(
            db, force=args.force, only_other=args.only_other, limit=args.limit
        )
        total = len(events)
        print(f"Model: {settings.gemini_model}  Events to categorize: {total}")
        if total == 0:
            return

        batch_size = max(1, settings.gemini_batch_size)
        counts: Counter[str] = Counter()
        labeled = 0
        failed_batches = 0
        quota_reached = False

        with GeminiCategorizer(
            settings.gemini_api_key,
            settings.gemini_model,
            timeout=settings.gemini_timeout_seconds,
            min_interval_seconds=settings.gemini_min_interval_seconds,
            max_retries=settings.gemini_max_retries,
        ) as categorizer:
            for start in range(0, total, batch_size):
                chunk = events[start : start + batch_size]
                to_classify = [
                    EventToClassify(
                        id=event.id,
                        title=event.title,
                        description=event.description,
                        source_category=event.source_category,
                        venue=event.venue,
                    )
                    for event in chunk
                ]
                try:
                    labels = categorizer.classify_batch(options, to_classify)
                except GeminiQuotaExceeded:
                    # The free-tier quota is used up. Everything committed so far
                    # is saved; stop cleanly rather than churning through the
                    # rest, and let the user resume when the quota resets.
                    quota_reached = True
                    break
                except GeminiError as exc:
                    failed_batches += 1
                    print(f"  batch {start}-{start + len(chunk)} failed: {exc}")
                    continue

                for event in chunk:
                    slug = labels.get(event.id)
                    category = by_slug.get(slug) if slug else None
                    if category is None:
                        continue
                    set_ai_category(event, category)
                    counts[slug] += 1
                    labeled += 1
                db.commit()
                print(f"  ...{min(start + batch_size, total)}/{total} ({labeled} labeled)")

        if quota_reached:
            remaining = total - labeled
            print(
                f"\nQuota reached. Labeled {labeled} this run; {remaining} still to go. "
                "Re-run the same command when your quota resets (usually the next "
                "day) and it will resume where it stopped."
            )
        print(f"\nLabeled {labeled} of {total} events by AI ({failed_batches} batches failed):")
        for slug, count in counts.most_common():
            print(f"  {slug}: {count}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
