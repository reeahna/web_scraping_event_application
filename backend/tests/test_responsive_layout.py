"""Every main page fits phone, tablet and desktop screens in real browsers.

Starts the app on a local port, fills it with realistic data (long titles,
several events, a signed-in member with saved events and alerts, an admin),
then opens each page at several screen sizes and fails if anything makes the
page scroll sideways, the most common way a layout breaks on a phone.

Browsers: Chromium by default. LAYOUT_BROWSERS="chromium,firefox,webkit" runs
the same checks in Firefox and WebKit (Safari's engine), which CI does in its
`browsers` job. A browser that is not installed is skipped, not failed.
LAYOUT_SCREENSHOT_DIR=<dir> also saves a full-page screenshot of every page at
every size, for a person to look over.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.core.permissions import REGISTERED_USER, SUPER_ADMINISTRATOR
from app.main import app as fastapi_app
from app.models.user_engagement import SavedEvent
from app.services.notifications import SEVERITY_INFO, notify

playwright_api = pytest.importorskip("playwright.sync_api")
uvicorn = pytest.importorskip("uvicorn")

# Phones (portrait and landscape), iPads (portrait and landscape, mini/Air
# and Pro), laptops and a wide monitor. Named so a failure and a screenshot
# say which one.
VIEWPORTS = {
    "phone-320": (320, 640),
    "phone-390": (390, 844),
    "phone-landscape-844": (844, 390),
    "ipad-portrait-768": (768, 1024),
    "ipad-air-portrait-820": (820, 1180),
    "ipad-landscape-1024": (1024, 768),
    "ipad-pro-portrait-1024": (1024, 1366),
    "ipad-air-landscape-1180": (1180, 820),
    "laptop-1280": (1280, 800),
    "desktop-1440": (1440, 900),
    "wide-1920": (1920, 1080),
}

BROWSERS = [
    name.strip()
    for name in os.environ.get("LAYOUT_BROWSERS", "chromium").split(",")
    if name.strip()
]

LONG_WORD = "Supercalifragilisticexpialidocious-Interdisciplinary-Symposium"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live_server():
    port = _free_port()
    config = uvicorn.Config(fastapi_app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("live server did not start")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture
def layout_data(make_city, make_website, make_event, make_user, db_session):
    city = make_city(
        name="Ann Arbor",
        slug="ann-arbor",
        timezone="America/Detroit",
        state_or_region="MI",
        country="US",
        universities=["University of Michigan"],
    )
    make_city(name="East Lansing", slug="east-lansing", state_or_region="MI", country="US")
    website = make_website(
        city,
        name="University Calendar",
        base_url="https://events.example.edu",
        is_active=True,
        approved_pattern={"pattern_name": "static_html"},
        source_display_name="University Calendar",
    )
    today = datetime.now(UTC).date()
    events = []
    titles = [
        "Jazz Night at the Union",
        f"{LONG_WORD} on Climate, Cities and the Future of Transit in the Great Lakes Region",
        "Farmers Market",
        "Film Screening: A Very Long Title That Wraps Across Several Lines on a Phone",
        "Open Mic",
        "Career Fair",
    ]
    for i, title in enumerate(titles):
        events.append(
            make_event(
                city,
                title=title,
                canonical_url=f"https://events.example.edu/e/{i}",
                website=website,
                start_date=today + timedelta(days=i + 1),
                end_date=today + timedelta(days=i + 1),
                venue=f"Michigan League Ballroom, Room {100 + i}" if i % 2 else LONG_WORD,
                address="911 N University Ave, Ann Arbor, MI 48109",
                description=("A long description of the event. " * 20) + LONG_WORD,
            )
        )
    # A past saved event exercises the "expired" section of Saved.
    past = make_event(
        city,
        title="Last Week's Lecture",
        canonical_url="https://events.example.edu/e/past",
        website=website,
        start_date=today - timedelta(days=7),
    )

    member = make_user(email="member@example.com", role_name=REGISTERED_USER)
    make_user(email="admin@example.com", role_name=SUPER_ADMINISTRATOR)
    for event in (events[0], events[1], past):
        db_session.add(SavedEvent(user_id=member.id, event_id=event.id))
    db_session.commit()
    for event in events[:3]:
        notify(
            db_session,
            notification_type="user_alert",
            severity=SEVERITY_INFO,
            title=f"New: {event.title}"[:255],
            message=f"{event.title} was added in Ann Arbor",
            recipients=[member],
            dedup_fingerprint=f"layout:{event.id}",
            related_resource_type="event",
            related_resource_id=event.id,
            action_url=f"/events/{event.id}",
        )
    return {"city": city, "event": events[1]}


def _session_cookie(client, login, email: str) -> dict:
    client.cookies.clear()
    login(email)
    from app.config import get_settings

    name = get_settings().session_cookie_name
    return {"name": name, "value": client.cookies.get(name)}


def _launch(pw, name: str):
    browser_type = getattr(pw, name)
    try:
        return browser_type.launch()
    except Exception as exc:  # not installed in this environment
        pytest.skip(f"{name} is not available: {str(exc).splitlines()[0]}")


# Reports what pokes past the right edge, so a failure names the element.
_OVERFLOW_JS = """
() => {
  const width = document.documentElement.clientWidth;
  const scroll = document.documentElement.scrollWidth;
  const culprits = [];
  const sticksOut = (el) => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.right > width + 1;
  };
  const scrollsInOwnBox = (el) => {
    for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
      const ox = getComputedStyle(p).overflowX;
      if (ox !== 'visible') return true;
    }
    return false;
  };
  const name = (el) => {
    const cls = typeof el.className === 'string' && el.className.trim()
      ? '.' + el.className.trim().split(/\\s+/).join('.') : '';
    return el.tagName.toLowerCase() + cls;
  };
  if (scroll > width + 1) {
    for (const el of document.querySelectorAll('body *')) {
      if (!sticksOut(el) || scrollsInOwnBox(el)) continue;
      if (getComputedStyle(el).position === 'fixed') continue;
      // Name the innermost things that stick out, with two ancestors for context.
      if ([...el.children].some(sticksOut)) continue;
      const path = [el.parentElement?.parentElement, el.parentElement, el]
        .filter((e) => e && e !== document.body).map(name).join(' > ');
      culprits.push(`${path} (right edge ${Math.round(el.getBoundingClientRect().right)}px)`);
      if (culprits.length >= 4) break;
    }
  }
  return {width, scroll, culprits};
}
"""


@pytest.mark.parametrize("browser_name", BROWSERS)
def test_pages_fit_every_screen(browser_name, live_server, layout_data, client, login):
    city, event = layout_data["city"], layout_data["event"]
    public = ["/", f"/city/{city.slug}", f"/events/{event.id}", "/auth/login",
              "/privacy", "/terms", "/no-such-page"]
    member_pages = ["/", f"/city/{city.slug}", f"/events/{event.id}", "/account",
                    "/account/saved", "/account/inbox", "/account/alerts"]
    admin_pages = ["/admin", "/admin/cities", f"/admin/cities/{city.id}",
                   "/admin/websites", "/admin/events", f"/admin/events/{event.id}",
                   "/admin/event-categories", "/admin/notifications", "/admin/scheduler"]
    visits = (
        [("guest", None, p) for p in public]
        + [("member", "member@example.com", p) for p in member_pages]
        + [("admin", "admin@example.com", p) for p in admin_pages]
    )
    cookies = {
        who: _session_cookie(client, login, email)
        for who, email, _ in visits
        if email
    }

    shots = os.environ.get("LAYOUT_SCREENSHOT_DIR")
    shot_dir = Path(shots) / browser_name if shots else None
    if shot_dir:
        shot_dir.mkdir(parents=True, exist_ok=True)

    problems: list[str] = []
    with playwright_api.sync_playwright() as pw:
        browser = _launch(pw, browser_name)
        try:
            for vp_name, (width, height) in VIEWPORTS.items():
                for who in ("guest", "member", "admin"):
                    context = browser.new_context(viewport={"width": width, "height": height})
                    if who in cookies:
                        cookie = cookies[who]
                        context.add_cookies([{**cookie, "url": live_server}])
                    page = context.new_page()
                    errors: list[str] = []
                    page.on("pageerror", lambda exc, errors=errors: errors.append(str(exc)))
                    for _, _, path in (v for v in visits if v[0] == who):
                        page.goto(live_server + path, wait_until="load")
                        result = page.evaluate(_OVERFLOW_JS)
                        label = f"{browser_name} {vp_name} {who} {path}"
                        if result["scroll"] > result["width"] + 1:
                            problems.append(
                                f"{label}: page is {result['scroll']}px wide in a "
                                f"{result['width']}px screen ({'; '.join(result['culprits'])})"
                            )
                        if shot_dir:
                            slug = path.strip("/").replace("/", "_") or "home"
                            page.screenshot(
                                path=str(shot_dir / f"{vp_name}--{who}--{slug}.png"),
                                full_page=True,
                            )
                    problems.extend(
                        f"{browser_name} {vp_name} {who}: script error {e}" for e in errors
                    )
                    context.close()
        finally:
            browser.close()

    assert not problems, "Layout problems:\n" + "\n".join(problems)

