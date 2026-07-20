"""Development command: onboard the Buskirk-Chumley Theater (Bloomington, IN)
as a real event source, end to end, through the same production services the
admin routes use — detection, manual pattern selection, draft configuration,
preview, approval, activation, and persistent extraction. No shortcuts: this
never writes `configuration`/`approved_pattern` directly.

Usage (from new_app/, with its venv active):

    python scripts/onboard_buskirk_chumley.py

Idempotent: reuses the city/website row if they already exist, and re-running
persistent extraction updates existing events rather than duplicating them.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.onboarding import ACTIVE, APPROVED, DETECTED, DRAFT, NEEDS_REVIEW, UNSUPPORTED
from app.database import SessionLocal
from app.models.city import City
from app.models.website import Website
from app.repositories.city import create_city
from app.repositories.website import create_website
from app.schemas.city import CityCreate
from app.schemas.extraction import FieldSelectorConfig, SiteConfiguration, TransformationRuleConfig
from app.schemas.website import WebsiteCreate
from app.services.extraction_runs import preview_extraction, run_detection, run_extraction
from app.services.website_configuration import approve_configuration, save_draft_configuration
from app.services.website_configuration import select_pattern as select_pattern_service
from app.services.websites import transition_website

CITY_NAME = "Bloomington"
CITY_SLUG = "bloomington-in"
CITY_TIMEZONE = "America/Indiana/Indianapolis"

WEBSITE_NAME = "Buskirk-Chumley Theater"
BASE_URL = "https://buskirkchumley.org"
LISTING_URL = "https://buskirkchumley.org/events/"

FIELD_SELECTORS = {
    "title": FieldSelectorConfig(kind="css", selector="div.details a"),
    "canonical_url": FieldSelectorConfig(kind="css", selector="div.details a", attribute="href"),
    "detail_link": FieldSelectorConfig(kind="css", selector="div.details a", attribute="href"),
    "venue": FieldSelectorConfig(kind="css", selector="div.details p"),
    "start_time": FieldSelectorConfig(kind="css", selector="div.details p"),
    "image": FieldSelectorConfig(kind="css", selector="div.thumb", attribute="style"),
    "source_category": FieldSelectorConfig(kind="css", selector="div.details span"),
    # Resolved against the *detail* page (see app.extraction.detail_pages) —
    # the listing card's date omits the year entirely ("24 / July / Friday"
    # split across separate <li> elements), so a real start_date can only be
    # derived from the detail page's "Fri Jul 24, 2026" text. This is not a
    # convenience: without it, start_date cannot be produced at all.
    "detail_start_datetime": FieldSelectorConfig(
        kind="css", selector="div.info li:has(i.fa-calendar)"
    ),
    "detail_description": FieldSelectorConfig(kind="css", selector=".confirm-description"),
}

TRANSFORMATIONS = [
    # "Doors: 6:30 PM / Show: 7:00 PM @ Buskirk-Chumley Theater" -> venue
    TransformationRuleConfig(
        field="venue", kind="regex_extract_group", params={"pattern": r"@\s*(.+)$", "group": 1}
    ),
    # Same raw text -> the show (not doors) start time.
    TransformationRuleConfig(
        field="start_time",
        kind="regex_extract_group",
        params={"pattern": r"Show:\s*([\d:]{3,8}\s*[AP]M)", "group": 1},
    ),
    # div.thumb's image is a CSS background, not an <img src>:
    # style="background:url('https://.../Holy_Grail.jpg') no-repeat ..."
    TransformationRuleConfig(
        field="image",
        kind="regex_extract_group",
        params={"pattern": r"url\('([^']+)'\)", "group": 1},
    ),
    # "Fri Jul 24, 2026 + Google Cal" -> just the date (the Google Calendar
    # link's own `dates=` param is a stale template placeholder on this real
    # site and must not be used as a date source).
    TransformationRuleConfig(
        field="start_datetime",
        kind="regex_extract_group",
        params={"pattern": r"^([A-Za-z]{3} [A-Za-z]{3} \d{1,2}, \d{4})", "group": 1},
    ),
]

CONFIG = SiteConfiguration(
    pattern_name="generic_html_cards",
    listing_url=LISTING_URL,
    timezone=CITY_TIMEZONE,
    event_container_selector="div.tile",
    field_selectors=FIELD_SELECTORS,
    date_formats=["%a %b %d, %Y"],
    time_formats=["%I:%M %p"],
    transformations=TRANSFORMATIONS,
    max_detail_fetches=30,
)


def _get_or_create_city(db) -> City:
    city = db.query(City).filter(City.slug == CITY_SLUG).first()
    if city is not None:
        print(f"Reusing existing city {city.name!r} (id={city.id}).")
        return city
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
    return city


def _get_or_create_website(db, city: City) -> Website:
    website = db.query(Website).filter(Website.base_url == BASE_URL).first()
    if website is not None:
        print(f"Reusing existing website {website.name!r} (id={website.id}).")
        return website
    website = create_website(
        db,
        WebsiteCreate(
            name=WEBSITE_NAME,
            city_id=city.id,
            base_url=BASE_URL,
            event_listing_url=LISTING_URL,
        ),
    )
    print(
        f"Created website {website.name!r} (id={website.id}), status={website.onboarding_status}."
    )
    return website


async def main() -> None:
    db = SessionLocal()
    try:
        city = _get_or_create_city(db)
        website = _get_or_create_website(db, city)

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
            # Detection auto-selected a different pattern with sufficient
            # confidence (e.g. wordpress_rest, from the wp-json/generator
            # signals every WordPress site exposes) — DETECTED isn't itself
            # a valid source state for manual pattern selection, so first
            # move it to NEEDS_REVIEW via the same generic status-transition
            # mechanism the admin "/{id}/status" route uses for exactly this
            # "I want to pick a different pattern" case.
            print(
                f"\nDetection selected a different pattern than requested; "
                f"moving DETECTED -> {NEEDS_REVIEW} for manual override."
            )
            website = transition_website(db, website, NEEDS_REVIEW)

        if website.onboarding_status in (DRAFT, NEEDS_REVIEW, UNSUPPORTED):
            print("\n--- Manually selecting generic_html_cards ---")
            website = select_pattern_service(db, website, pattern_name="generic_html_cards")
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
        if preview.errors:
            print("Rejection reasons (first 5):")
            for err in preview.errors[:5]:
                print(" -", err)
        if preview.warnings:
            print("Warnings:", preview.warnings)

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
        assert website.onboarding_status == APPROVED
        assert website.is_active is False

        print("\n--- Activating website ---")
        website = transition_website(db, website, ACTIVE)
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
    finally:
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
