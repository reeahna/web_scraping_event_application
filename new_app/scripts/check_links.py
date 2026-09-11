"""Whole-site link and button checker.

Logs in as a throwaway super admin, visits every parameterless GET page plus
every internal link it discovers, and inspects every form / HTMX target and
button on each page. It reports, and exits non-zero on, two hard failures:

  * BROKEN GET   an internal link fetched -> HTTP >= 400 (or no route at all)
  * DEAD TARGET  a form action / hx-* / formaction whose (method, path) matches
                 no registered route, or matches only with the wrong method

It also lists (without failing) anchors/buttons that go nowhere (href="#",
empty, no handler). It never issues a non-GET request and never GETs an
action-shaped URL (logout/delete/activate/...), so its only side effect is the
temp admin account it creates and then deletes.

Usage (from new_app/, venv active):

    python scripts/check_links.py               # whole site (admin + public)
    python scripts/check_links.py --public-only  # skip /admin and /account

This verifies routing and server-rendered output only; it does not run page
JavaScript, so it will not catch a broken map render or an HTMX swap that
misbehaves. It shares its crawl engine with tests/test_link_integrity.py.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bs4 import BeautifulSoup

# Never fetch a GET link whose path hints at a state change or would end the
# session; such targets are still verified to resolve, just not rendered.
DANGER = re.compile(
    r"(logout|/delete|/remove|/deactivate|/activate|/archive|/approve|/reject"
    r"|/purge|/reset|/run\b|/import\b|/retry|/cancel|/duplicate|/merge)",
    re.IGNORECASE,
)
PER_TEMPLATE_CAP = 3
MAX_FETCHES = 5000
_HX = {"hx-get": "GET", "hx-post": "POST", "hx-delete": "DELETE",
       "hx-put": "PUT", "hx-patch": "PATCH"}


@dataclass
class Report:
    broken: list[tuple] = field(default_factory=list)  # (url, status, template)
    dead: list[tuple] = field(default_factory=list)     # (target, reason, where, page)
    empties_by_page: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    template_counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    fetched_ok: int = 0

    @property
    def ok(self) -> bool:
        return not self.broken and not self.dead


def build_routes(routes) -> list[tuple]:
    """Flatten the app's included routers into (path_regex, methods, template)."""
    collected: list[tuple] = []
    for route in routes:
        regex = getattr(route, "path_regex", None)
        methods = getattr(route, "methods", None)
        if regex is not None and methods:
            collected.append((regex, set(methods), route.path))
        original = getattr(route, "original_router", None)
        if original is not None and getattr(original, "routes", None):
            collected.extend(build_routes(original.routes))
        elif getattr(route, "routes", None):
            collected.extend(build_routes(route.routes))
    return collected


def match_route(routes, path, method) -> tuple[str | None, bool]:
    """Return (template, method_ok); template is None when no path matches."""
    path_match = None
    for regex, methods, template in routes:
        if regex.match(path):
            path_match = template
            if method in methods:
                return template, True
    return path_match, False


def is_internal(url: str) -> bool:
    parts = urlsplit(url)
    return (not parts.scheme and not parts.netloc) or parts.netloc == "testserver"


def extract(soup: BeautifulSoup, base: str):
    """Return (get_links, action_targets, empties)."""
    get_links, actions, empties = set(), [], []

    for a in soup.find_all("a"):
        href = (a.get("href") or "").strip()
        label = " ".join(a.get_text().split())[:40] or "(no text)"
        if not href or href == "#" or href.startswith("javascript:"):
            if not any(a.get(k) for k in _HX):
                empties.append(f"<a> '{label}'")
            continue
        if href.startswith(("mailto:", "tel:", "#")):
            continue
        get_links.add(urljoin(base, href))

    for form in soup.find_all("form"):
        method = (form.get("method") or "get").upper()
        action = (form.get("action") or "").strip()
        actions.append((method, urljoin(base, action) if action else base, "form"))

    for attr, method in _HX.items():
        for el in soup.find_all(attrs={attr: True}):
            val = (el.get(attr) or "").strip()
            if val:
                actions.append((method, urljoin(base, val), attr))

    for btn in soup.find_all("button"):
        fa = (btn.get("formaction") or "").strip()
        if fa:
            actions.append(("POST", urljoin(base, fa), "formaction"))
            continue
        label = " ".join(btn.get_text().split())[:40] or "(no text)"
        has_action = (
            btn.get("type") == "submit"
            or any(btn.get(k) for k in _HX)
            or btn.get("onclick")
            or btn.get("name")
        )
        parent_a = btn.find_parent("a")
        wrapped = parent_a is not None and (parent_a.get("href") or "").strip() not in ("", "#")
        if not has_action and not btn.find_parent("form") and not wrapped:
            empties.append(f"<button> '{label}'")

    return get_links, actions, empties


def crawl(client, routes, *, seeds, public_only=False,
          per_template_cap=PER_TEMPLATE_CAP, max_fetches=MAX_FETCHES) -> Report:
    report = Report()
    checked_targets: set[tuple[str, str]] = set()

    def blocked(path: str) -> bool:
        return public_only and (path.startswith("/admin") or path.startswith("/account"))

    queue = [u for u in dict.fromkeys(["/"] + list(seeds)) if not blocked(u)]
    seen = set(queue)
    while queue and report.fetched_ok < max_fetches:
        url = queue.pop(0)
        path = urlsplit(url).path
        if DANGER.search(path) or blocked(path):
            continue
        template, _ = match_route(routes, path, "GET")
        if template is None:
            report.broken.append((url, "no route", "GET"))
            continue
        if report.template_counts[template] >= per_template_cap:
            continue
        report.template_counts[template] += 1

        resp = client.get(url, follow_redirects=False)
        if resp.status_code >= 400:
            report.broken.append((url, resp.status_code, template))
            continue
        report.fetched_ok += 1
        if resp.status_code in (301, 302, 303, 307, 308):
            loc = resp.headers.get("location", "")
            if loc and is_internal(loc):
                nxt = urljoin(url, loc)
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append(nxt)
            continue
        if "text/html" not in resp.headers.get("content-type", ""):
            continue

        soup = BeautifulSoup(resp.text, "html.parser")
        get_links, actions, empties = extract(soup, url)
        for e in empties:
            report.empties_by_page[url].append(e)
        for link in get_links:
            if is_internal(link) and link not in seen:
                seen.add(link)
                queue.append(link)
        for method, turl, where in actions:
            if not is_internal(turl):
                continue
            tpath = urlsplit(turl).path
            key = (method, tpath)
            if key in checked_targets:
                continue
            checked_targets.add(key)
            tmpl, ok = match_route(routes, tpath, method)
            if tmpl is None:
                report.dead.append((f"{method} {tpath}", "no route", where, url))
            elif not ok:
                report.dead.append((f"{method} {tpath}", "wrong method", where, url))
    return report


def seed_paths(routes, *, exclude=frozenset()) -> list[str]:
    return sorted({
        t for _, m, t in routes
        if "GET" in m and "{" not in t and "*" not in t and t not in exclude
    })


def print_report(report: Report, routes) -> None:
    print(f"\n=== {report.fetched_ok} pages fetched, "
          f"{len(report.template_counts)} distinct route templates hit ===\n")
    print(f"BROKEN GET LINKS ({len(report.broken)}):")
    for url, status, tmpl in sorted(report.broken):
        print(f"  [{status}] {url}   (template: {tmpl})")
    print("  none" if not report.broken else "")

    print(f"\nDEAD ACTION TARGETS ({len(report.dead)}):")
    for target, reason, where, page in sorted(report.dead):
        print(f"  {target}  <- {reason} ({where}) on {page}")
    print("  none" if not report.dead else "")

    get_templates = {t for _, m, t in routes if "GET" in m}
    unreached = sorted(get_templates - set(report.template_counts))
    print(f"\nGET PAGE ROUTES NEVER REACHED ({len(unreached)} of {len(get_templates)}):")
    for t in unreached:
        print(f"  {t}")

    total_empty = sum(len(v) for v in report.empties_by_page.values())
    print(f"\nEMPTY / '#' ANCHORS & BUTTONS ({total_empty} on {len(report.empties_by_page)} pages):")
    for page, items in sorted(report.empties_by_page.items()):
        print(f"  {page}")
        for it in sorted(set(items)):
            print(f"      {it}")


TEMP_EMAIL = "link-check-bot@internal.test"
TEMP_PASSWORD = "link-check-bot-pw-9137"
_DOCS = {"/openapi.json", "/redoc", "/docs", "/docs/oauth2-redirect"}


def _create_temp_admin() -> int:
    from app.core.permissions import SUPER_ADMINISTRATOR
    from app.core.security import hash_password
    from app.database import SessionLocal
    from app.models.role import Role
    from app.models.user import User
    from app.models.user_role import UserRole

    db = SessionLocal()
    try:
        existing = db.query(User).filter(User.email == TEMP_EMAIL).one_or_none()
        if existing:
            db.query(UserRole).filter(UserRole.user_id == existing.id).delete()
            db.delete(existing)
            db.commit()
        user = User(email=TEMP_EMAIL, hashed_password=hash_password(TEMP_PASSWORD), is_active=True)
        db.add(user)
        db.commit()
        db.refresh(user)
        role = db.query(Role).filter(Role.name == SUPER_ADMINISTRATOR).one()
        db.add(UserRole(user_id=user.id, role_id=role.id))
        db.commit()
        return user.id
    finally:
        db.close()


def _delete_temp_admin(user_id: int) -> None:
    from app.database import SessionLocal
    from app.models.user import User
    from app.models.user_role import UserRole

    db = SessionLocal()
    try:
        db.query(UserRole).filter(UserRole.user_id == user_id).delete()
        user = db.get(User, user_id)
        if user:
            db.delete(user)
        db.commit()
    finally:
        db.close()


def login(client, email: str, password: str) -> None:
    from app.config import get_settings

    settings = get_settings()
    client.get("/auth/login")
    csrf = client.cookies.get(settings.csrf_cookie_name)
    resp = client.post(
        "/auth/login",
        data={"email": email, "password": password, "csrf_token": csrf},
        follow_redirects=False,
    )
    if resp.status_code not in (302, 303) or not client.cookies.get(settings.session_cookie_name):
        raise SystemExit(f"login failed (status {resp.status_code})")


def main() -> None:
    parser = argparse.ArgumentParser(description="Check every site link and button resolves.")
    parser.add_argument("--public-only", action="store_true",
                        help="Skip /admin and /account (check only public pages).")
    args = parser.parse_args()

    from starlette.testclient import TestClient

    import app.models  # noqa: F401  (register models)
    from app.main import app as fastapi_app

    routes = build_routes(fastapi_app.router.routes)
    seeds = seed_paths(routes, exclude=_DOCS)

    user_id = _create_temp_admin()
    try:
        with TestClient(fastapi_app, base_url="http://testserver") as client:
            login(client, TEMP_EMAIL, TEMP_PASSWORD)
            report = crawl(client, routes, seeds=seeds, public_only=args.public_only)
    finally:
        _delete_temp_admin(user_id)

    print_report(report, routes)
    if not report.ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
