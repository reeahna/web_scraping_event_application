"""Guardrail: no page links to a dead URL and no form/button targets a route
that does not exist. Shares the crawl engine with scripts/check_links.py.

Against the (data-empty) test database this exercises every parameterless page
and every form / HTMX / button target on it. It will not reach entity detail
pages that need a row to exist, so it is a floor, not a full audit — run
`python scripts/check_links.py` against a populated database for the whole site.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import check_links  # noqa: E402

from app.main import app as fastapi_app  # noqa: E402

LINK_ADMIN_EMAIL = "link-integrity-admin@example.com"
LINK_ADMIN_PASSWORD = "correct-horse-battery"


def test_no_broken_links_or_dead_targets(client, make_super_admin):
    make_super_admin(email=LINK_ADMIN_EMAIL, password=LINK_ADMIN_PASSWORD)
    check_links.login(client, LINK_ADMIN_EMAIL, LINK_ADMIN_PASSWORD)

    routes = check_links.build_routes(fastapi_app.router.routes)
    seeds = check_links.seed_paths(routes, exclude=check_links._DOCS)
    report = check_links.crawl(client, routes, seeds=seeds)

    # It actually walked the site (guards against a silent no-op).
    assert report.fetched_ok > 10
    assert report.broken == [], f"broken links: {report.broken}"
    assert report.dead == [], f"dead form/HTMX/button targets: {report.dead}"
