"""Canonical permission and default-role catalog.

Single source of truth, reused by the Alembic data seed, the CLI bootstrap
script (`scripts/create_superadmin.py`), and the test suite — so the three
never drift apart.
"""

SUPER_ADMINISTRATOR = "Super Administrator"
ADMINISTRATOR = "Administrator"
EDITOR = "Editor"
VIEWER = "Viewer"

DEFAULT_ROLES: tuple[str, ...] = (SUPER_ADMINISTRATOR, ADMINISTRATOR, EDITOR, VIEWER)

PERMISSIONS: dict[str, str] = {
    "cities.view": "View cities",
    "cities.create": "Create cities",
    "cities.update": "Update cities",
    "cities.activate": "Activate or deactivate cities",
    "cities.delete": "Delete cities",
    "sites.view": "View websites/sources",
    "sites.create": "Create websites/sources",
    "sites.update": "Update websites/sources",
    "sites.test": "Test-run a website/source",
    "sites.approve": "Approve a website/source",
    "sites.activate": "Activate or deactivate websites/sources",
    "sites.delete": "Delete websites/sources",
    # Scraper-first model: events are populated by scrapers, not typed in by
    # hand, so there is deliberately no general "events.create"/"events.update".
    # Scraped fields (title, dates, venue, etc.) are read-only; humans only
    # review, curate, and correct specific attributes.
    "events.view": "View events",
    "events.review": "Review scraped events pending approval",
    "events.activate": "Activate or deactivate events",
    "events.archive": "Archive events",
    "events.delete": "Delete events",
    "events.override_category": "Override the category assigned to a scraped event",
    "events.correct_location": "Correct coordinates or location details for an event",
    "events.resolve_duplicates": "Resolve duplicate scraped events",
    "events.view_provenance": "View the scraping source/provenance of an event",
    "users.view": "View users",
    "users.update": "Update users, including activation/deactivation",
    "roles.manage": "Manage roles, permissions, and role/permission assignments",
    "reports.view": "View reports",
    "settings.manage": "Manage application settings",
}

_ALL_CODES: tuple[str, ...] = tuple(PERMISSIONS.keys())

# Default role -> permission codes. Super Administrator is granted every
# permission as explicit rows (not a wildcard) so effective-permission queries
# and audits never need special-casing for it.
DEFAULT_ROLE_PERMISSIONS: dict[str, tuple[str, ...]] = {
    SUPER_ADMINISTRATOR: _ALL_CODES,
    ADMINISTRATOR: tuple(code for code in _ALL_CODES if code != "roles.manage"),
    EDITOR: (
        "cities.view",
        "cities.create",
        "cities.update",
        "sites.view",
        "sites.create",
        "sites.update",
        "sites.test",
        "events.view",
        "events.review",
        "events.activate",
        "events.override_category",
        "events.correct_location",
        "events.resolve_duplicates",
        "events.view_provenance",
        "reports.view",
    ),
    VIEWER: (
        "cities.view",
        "sites.view",
        "events.view",
        "users.view",
        "reports.view",
    ),
}
