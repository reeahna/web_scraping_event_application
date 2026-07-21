"""Development command: onboard Indiana University Auditorium (Bloomington,
IN) as a real event source, end to end, through the same production services
the admin routes use — detection, (manual pattern selection only if
detection doesn't already pick generic_html_cards), draft configuration,
preview, approval, activation, and persistent extraction. No shortcuts: this
never writes `configuration`/`approved_pattern` directly, and it performs no
extraction itself — that's entirely app.services.extraction_runs's job.

Usage (from new_app/, with its venv active):

    python scripts/onboard_iu_auditorium.py

Idempotent: reuses the city/website row if they already exist (the city row
is shared with scripts/onboard_buskirk_chumley.py — same Bloomington), and
re-running persistent extraction updates existing events rather than
duplicating them.

Known limitation: IU Auditorium's listing page renders some events with a
date *range* (e.g. "Sep 12 - 13 , 2026") rather than a single date. This
pattern's generic date parsing only handles a single parseable date per
event — range events are safely rejected by the existing required-start-date
validation (never guessed at), not silently dropped or crashed on.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.onboarding import (
    ACTIVE,
    APPROVED,
    DETECTED,
    DRAFT,
    NEEDS_REVIEW,
    UNSUPPORTED,
)
from app.database import SessionLocal
from app.models.city import City
from app.models.event import Event
from app.models.event_provenance import EventProvenance
from app.models.website import Website
from app.repositories.city import create_city
from app.repositories.website import create_website
from app.schemas.city import CityCreate
from app.schemas.extraction import FieldSelectorConfig, SiteConfiguration
from app.schemas.website import WebsiteCreate
from app.services.extraction_runs import preview_extraction, run_detection, run_extraction
from app.services.website_configuration import approve_configuration, save_draft_configuration
from app.services.website_configuration import select_pattern as select_pattern_service
from app.services.websites import transition_website

CITY_NAME = "Bloomington"
CITY_SLUG = "bloomington-in"
CITY_TIMEZONE = "America/Indiana/Indianapolis"

WEBSITE_NAME = "Indiana University Auditorium"
SOURCE_DISPLAY_NAME = "Indiana University Auditorium"
BASE_URL = "https://www.iuauditorium.com"
LISTING_URL = "https://www.iuauditorium.com/events"

PATTERN_NAME = "generic_html_cards"

# Determined by fetching the live listing page through the same SSRF-safe
# fetch path the extraction engine itself uses, then inspecting the actual
# card markup (see the card dump this script's author used — not committed,
# just the real DOM). Each selector is a specific semantic class, never
# nth-child/deep-chain/generated-numeric-class/broad a[href].
FIELD_SELECTORS = {
    "title": FieldSelectorConfig(kind="css", selector="h3.m-eventItem__title a"),
    "canonical_url": FieldSelectorConfig(
        kind="css", selector="h3.m-eventItem__title a", attribute="href"
    ),
    # Only present on single-date cards; absent entirely on range-date cards
    # (e.g. "Sep 12 - 13 , 2026"), which is exactly how those are meant to
    # fall through to the existing required-start-date rejection below —
    # no site-specific range parser, no invented date.
    "start_datetime": FieldSelectorConfig(kind="css", selector=".m-date__singleDate"),
    "image": FieldSelectorConfig(
        kind="css", selector="div.m-venueframework-eventslist__thumb img", attribute="src"
    ),
    "description": FieldSelectorConfig(kind="css", selector="h4.m-eventItem__tagline"),
}

# resolve_css's own text extraction (BeautifulSoup get_text(" ", strip=True))
# already collapses each span's whitespace and joins with a single space, so
# ".m-date__singleDate" consistently yields e.g. "Tuesday | Oct 6 , 2026" —
# no separate collapse-whitespace transformation is needed on top of that.
# %A/%b/%d/%Y matches that exact shape (weekday name, pipe, abbreviated
# month, day, comma, year) without a custom parser.
DATE_FORMATS = ["%A | %b %d , %Y"]

CONFIG = SiteConfiguration(
    pattern_name=PATTERN_NAME,
    listing_url=LISTING_URL,
    timezone=CITY_TIMEZONE,
    event_container_selector="div.m-venueframework-eventslist__item",
    field_selectors=FIELD_SELECTORS,
    date_formats=DATE_FORMATS,
    pagination={"strategy": "none"},
    max_detail_fetches=0,
    required_fields=["title", "start_date", "canonical_url"],
)


def _get_or_create_city(db) -> tuple[City, bool]:
    city = db.query(City).filter(City.slug == CITY_SLUG).first()
    if city is not None:
        print(f"Reusing existing city {city.name!r} (id={city.id}).")
        return city, False
    city = create_city(
        db,
        CityCreate(
            name=CITY_NAME,
            slug=CITY_SLUG,
            state_or_region="Indiana",
            country="USA",
            timezone=CITY_TIMEZONE,
        ),
    )
    print(f"Created city {city.name!r} (id={city.id}).")
    return city, True


def _get_or_create_website(db, city: City) -> tuple[Website, bool]:
    website = db.query(Website).filter(Website.base_url == BASE_URL).first()
    if website is not None:
        print(f"Reusing existing website {website.name!r} (id={website.id}).")
        return website, False
    website = create_website(
        db,
        WebsiteCreate(
            name=WEBSITE_NAME,
            city_id=city.id,
            base_url=BASE_URL,
            event_listing_url=LISTING_URL,
            source_display_name=SOURCE_DISPLAY_NAME,
        ),
    )
    print(
        f"Created website {website.name!r} (id={website.id}), status={website.onboarding_status}."
    )
    return website, True


async def main() -> None:
    db = SessionLocal()
    try:
        city, _ = _get_or_create_city(db)
        website, _ = _get_or_create_website(db, city)

        if website.onboarding_status == DRAFT:
            print("\n--- Running detection ---")
            result = await run_detection(db, website)
            db.refresh(website)
            print(
                f"detection status={result.status} pattern={result.pattern} "
                f"onboarding_status={website.onboarding_status}"
            )
            print(f"evidence={result.evidence}")

        if website.onboarding_status == DETECTED:
            detected_pattern = (website.proposed_pattern or {}).get("detection", {}).get(
                "pattern_name"
            )
            if detected_pattern == PATTERN_NAME:
                print(f"\nDetection selected '{PATTERN_NAME}' — continuing without override.")
            else:
                print(
                    f"\nDetection selected '{detected_pattern}', not '{PATTERN_NAME}'; "
                    f"moving DETECTED -> {NEEDS_REVIEW} for manual override."
                )
                website = transition_website(db, website, NEEDS_REVIEW)

        if website.onboarding_status in (DRAFT, NEEDS_REVIEW, UNSUPPORTED):
            print(f"\n--- Manually selecting {PATTERN_NAME} ---")
            website = select_pattern_service(db, website, pattern_name=PATTERN_NAME)
            print(f"onboarding_status={website.onboarding_status}")

        print("\n--- Saving draft configuration ---")
        website = save_draft_configuration(db, website, CONFIG)
        print(f"configuration_version={website.configuration_version}")

        print("\n--- Running preview ---")
        preview = await preview_extraction(db, website)
        print(
            f"preview status={preview.status} found={preview.events_found} "
            f"valid={preview.events_valid} rejected={preview.events_rejected}"
        )
        missing_date_rejections = sum(
            1 for err in preview.errors if "start date" in err
        )
        print(f"rejections due to missing/unparseable start date (incl. date ranges): "
              f"{missing_date_rejections}")
        if preview.errors:
            print("Rejection reasons (first 8):")
            for err in preview.errors[:8]:
                print(" -", err)
        if preview.warnings:
            print("Warnings:", preview.warnings)

        if preview.events_valid == 0:
            print("\nZero candidates validated — refusing to approve.")
            return
        if preview.status not in ("success", "partial"):
            print("\nPreview did not succeed — stopping before approval.")
            return

        print("\n--- Approving configuration ---")
        # approved_by_user_id=None: this is a dev script, not an admin-session
        # action; approve_configuration only requires *a* value for the audit
        # column, which is nullable (ForeignKey(..., ondelete="SET NULL")).
        website = approve_configuration(db, website, approved_by_user_id=None)
        print(
            f"onboarding_status={website.onboarding_status} is_active={website.is_active} "
            f"(approval alone must not activate)"
        )
        assert website.onboarding_status == APPROVED or website.onboarding_status == ACTIVE
        if website.onboarding_status == APPROVED:
            assert website.is_active is False

        if website.onboarding_status != ACTIVE:
            print("\n--- Activating website ---")
            website = transition_website(db, website, ACTIVE)
        else:
            print("\nWebsite already active.")
        print(f"onboarding_status={website.onboarding_status} is_active={website.is_active}")

        print("\n--- Running persistent extraction (1st run) ---")
        first = await run_extraction(db, website, triggered_by_user_id=None)
        print(
            f"status={first.status} found={first.events_found} valid={first.events_valid} "
            f"inserted={first.events_inserted} updated={first.events_updated} "
            f"duplicates_skipped={first.duplicates_skipped}"
        )

        print("\n--- Running persistent extraction (2nd run, expect updates not duplicates) ---")
        second = await run_extraction(db, website, triggered_by_user_id=None)
        print(
            f"status={second.status} found={second.events_found} valid={second.events_valid} "
            f"inserted={second.events_inserted} updated={second.events_updated} "
            f"duplicates_skipped={second.duplicates_skipped}"
        )
        assert second.events_inserted == 0, "second run must not insert new events"

        event_count = db.query(Event).filter(Event.website_id == website.id).count()
        provenance_count = (
            db.query(EventProvenance).filter(EventProvenance.website_id == website.id).count()
        )
        print(f"\nEvent rows for this website: {event_count}")
        print(f"EventProvenance rows for this website: {provenance_count}")
    finally:
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
