"""The interactive parts of Bulletin work in real browsers.

tests/test_responsive_layout.py checks that every page fits the screen; this
file clicks, types and submits. Each test drives one flow through a real
browser against the app on a local port, with seeded test data (never
production), on a phone and a desktop screen.

Engines: Chromium by default; LAYOUT_BROWSERS="chromium,firefox,webkit" runs the
same tests in Firefox and WebKit (Safari's engine). CI runs one engine per
`browsers (...)` job. An engine that is not installed is skipped. See
tests/browser_support.py for the other environment variables.

Every page a test opens is also watched for script errors and Content Security
Policy refusals (the CSP allows no inline script), and either fails the test.

Two browser features a headless test browser cannot really use are replaced
with a recording stand-in, and only those: the clipboard (permissions differ
by engine) and the device share sheet (there is none). The page's own code
still decides when to show the buttons and what to send.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest

from app.core.permissions import REGISTERED_USER, SUPER_ADMINISTRATOR
from app.models.city import City
from app.models.extraction_run import ExtractionRun
from app.models.user_engagement import SavedEvent
from tests.browser_support import BROWSERS, launch, session_cookie

playwright_api = pytest.importorskip("playwright.sync_api")
expect = playwright_api.expect

VIEWPORTS = {"phone": (390, 844), "desktop": (1440, 900)}

# Runs before any page script: reports CSP refusals as console errors, which
# every engine passes to Playwright, so the watcher below sees them.
_WATCH_CSP = """
document.addEventListener('securitypolicyviolation', (e) => {
  console.error('CSP refused ' + e.violatedDirective + ' ' + (e.blockedURI || 'inline'));
});
"""

# Recording stand-ins for the clipboard and the share sheet.
_FAKE_CLIPBOARD = """
window.__copied = [];
Object.defineProperty(Navigator.prototype, 'clipboard', {
  configurable: true,
  get() { return { writeText: async (text) => { window.__copied.push(text); } }; },
});
"""
_FAKE_SHARE_SHEET = """
window.__shared = [];
Navigator.prototype.share = async function (data) { window.__shared.push(data); };
Navigator.prototype.canShare = function () { return true; };
"""
_NO_SHARE_SHEET = """
Object.defineProperty(Navigator.prototype, 'share', { configurable: true, value: undefined });
"""


@pytest.fixture(scope="module", params=BROWSERS)
def browser(request):
    with playwright_api.sync_playwright() as pw:
        engine = launch(pw, request.param)
        try:
            yield engine
        finally:
            engine.close()


@pytest.fixture(params=list(VIEWPORTS))
def screen(request):
    return request.param


@pytest.fixture
def site(make_city, make_website, make_event, make_category, make_user, db_session):
    """One town with two categories of events, a second town, a member with a
    saved event and an admin."""
    today = datetime.now(UTC).date()
    ann_arbor = make_city(
        name="Ann Arbor", slug="ann-arbor", timezone="America/Detroit",
        state_or_region="MI", country="US", universities=["University of Michigan"],
    )
    lansing = make_city(
        name="East Lansing", slug="east-lansing", timezone="America/Detroit",
        state_or_region="MI", country="US", universities=["Michigan State University"],
    )
    source = make_website(
        ann_arbor, name="University Calendar", base_url="https://events.example.edu",
        is_active=True, approved_pattern={"pattern_name": "static_html"},
        source_display_name="University Calendar",
    )
    lansing_source = make_website(
        lansing, name="MSU Calendar", base_url="https://msu.example.edu",
        is_active=True, approved_pattern={"pattern_name": "static_html"},
    )
    concerts = make_category(name="Browser Test Concerts", slug="browser-test-concerts")
    lectures = make_category(name="Browser Test Lectures", slug="browser-test-lectures")
    events = [
        make_event(
            ann_arbor, title=title, canonical_url=f"https://events.example.edu/e/{i}",
            website=source, category=category, start_date=today + timedelta(days=i + 1),
            venue="Michigan League", address="911 N University Ave, Ann Arbor, MI",
        )
        for i, (title, category) in enumerate([
            ("Jazz Night at the Union", concerts),
            ("Orchestra in the Park", concerts),
            ("Lecture on Great Lakes Transit", lectures),
        ])
    ]
    make_event(
        lansing, title="Spartan Open Mic", canonical_url="https://msu.example.edu/e/1",
        website=lansing_source, start_date=today + timedelta(days=2),
    )
    # A finished check, so the admin Websites list shows a system timestamp.
    finished = datetime.now(UTC) - timedelta(hours=2)
    db_session.add(ExtractionRun(
        website_id=source.id, run_type="manual", status="success",
        source_url=source.base_url, started_at=finished - timedelta(minutes=1),
        completed_at=finished,
    ))
    member = make_user(email="member@example.com", role_name=REGISTERED_USER)
    make_user(email="admin@example.com", role_name=SUPER_ADMINISTRATOR)
    db_session.add(SavedEvent(user_id=member.id, event_id=events[2].id))
    db_session.commit()
    return {
        "town": ann_arbor, "events": events, "concerts": concerts, "lectures": lectures,
        "member": member,
    }


@pytest.fixture
def open_page(browser, screen, live_server, client, login):
    """open_page(as_who=None, init_scripts=(), **context_options) -> a Page
    already at the site's address, signed in as "member" or "admin" if asked.
    Fails the test afterwards if any page it opened had a script error or a
    CSP refusal."""
    width, height = VIEWPORTS[screen]
    contexts = []
    problems: list[str] = []
    cookies: dict[str, dict] = {}

    def _open(as_who: str | None = None, init_scripts=(), **options):
        context = browser.new_context(
            viewport={"width": width, "height": height}, base_url=live_server, **options
        )
        contexts.append(context)
        if as_who:
            if as_who not in cookies:
                cookies[as_who] = session_cookie(client, login, f"{as_who}@example.com")
            context.add_cookies([{**cookies[as_who], "url": live_server}])
        for script in (_WATCH_CSP, *init_scripts):
            context.add_init_script(script)
        page = context.new_page()
        page.on("pageerror", lambda exc: problems.append(f"{page.url}: script error {exc}"))
        page.on(
            "console",
            lambda msg: msg.type == "error"
            and problems.append(f"{page.url}: console error {msg.text}"),
        )
        return page

    yield _open
    for context in contexts:
        context.close()
    assert not problems, "\n".join(problems)


def _results_count(page) -> int:
    text = page.locator("#results").inner_text()
    return int(re.match(r"\d+", text).group())


# ---------------------------------------------------------------- public pages


def test_town_search_suggests_and_opens_a_town(open_page, site):
    page = open_page()
    page.goto("/")
    search = page.locator("#city-search")
    suggestions = page.locator("#city-suggestions")

    # Typing offers matching towns; the keyboard picks one.
    search.press_sequentially("Ann")
    expect(suggestions).to_be_visible()
    expect(search).to_have_attribute("aria-expanded", "true")
    expect(suggestions.locator(".suggestion-name")).to_have_text(["Ann Arbor, MI"])
    search.press("ArrowDown")
    expect(search).to_have_attribute("aria-activedescendant", "city-suggestion-0")
    search.press("Enter")
    page.wait_for_url("**/city/ann-arbor")

    # A school name finds its town too, and a click (or tap) opens it.
    page.goto("/")
    search.press_sequentially("Michigan State")
    suggestions.get_by_text("East Lansing, MI").click()
    page.wait_for_url("**/city/east-lansing")

    # Escape closes the list without leaving the page.
    page.goto("/")
    search.press_sequentially("Mich")
    expect(suggestions).to_be_visible()
    search.press("Escape")
    expect(suggestions).to_be_hidden()
    expect(search).to_have_attribute("aria-expanded", "false")

    # No match: Enter submits the plain search instead.
    search.fill("")
    search.press_sequentially("Nowhereville")
    expect(suggestions).to_be_hidden()
    search.press("Enter")
    page.wait_for_url(re.compile(r"\?q=Nowhereville"))


def test_town_search_works_without_javascript(open_page, site):
    page = open_page(java_script_enabled=False)
    page.goto("/")
    page.locator("#city-search").fill("Ann")
    page.locator("#city-search").press("Enter")
    page.wait_for_url(re.compile(r"\?q=Ann"))
    page.get_by_role("link", name=re.compile("Ann Arbor")).first.click()
    page.wait_for_url("**/city/ann-arbor")


def test_town_filters_apply_and_clear(open_page, site):
    page = open_page()
    page.goto("/city/ann-arbor")
    expect(page.locator("#results")).to_have_text(re.compile(r"^3 events"))
    more = page.locator("details.filter-more")
    expect(more).not_to_have_attribute("open", "")

    # Pick a category from the folded-away filters.
    page.locator("summary.filter-more-toggle").click()
    expect(page.locator("#category_id")).to_be_visible()
    page.locator("#category_id").select_option(label="Browser Test Concerts")
    page.get_by_role("button", name="Apply filters").click()
    page.wait_for_url(re.compile(rf"category_id={site['concerts'].id}"))
    assert _results_count(page) == 2
    # An applied filter keeps the panel open so it is never out of sight.
    expect(more).to_have_attribute("open", "")
    expect(page.locator(".filter-count")).to_contain_text("1")

    page.locator(".results-head .filter-clear-link").click()
    page.wait_for_url(re.compile(r"/city/ann-arbor$"))
    assert _results_count(page) == 3

    # Hiding a category removes its events.
    page.locator("summary.filter-more-toggle").click()
    page.locator("details.filter-exclude > summary").click()
    page.locator(
        f"input[name=exclude_category][value='{site['lectures'].id}']"
    ).check()
    page.get_by_role("button", name="Apply filters").click()
    page.wait_for_url(re.compile(r"exclude_category="))
    assert _results_count(page) == 2
    expect(page.get_by_text("Lecture on Great Lakes Transit")).to_have_count(0)

    # Search by title.
    page.goto("/city/ann-arbor")
    page.locator("#q").fill("Orchestra")
    page.locator("#q").press("Enter")
    page.wait_for_url(re.compile(r"q=Orchestra"))
    assert _results_count(page) == 1


def test_copy_link_copies_the_event_address(open_page, site):
    event = site["events"][0]
    page = open_page(init_scripts=[_FAKE_CLIPBOARD])
    page.goto(f"/events/{event.id}")
    share = page.locator(".event-share")
    address = share.get_attribute("data-share-url")
    assert address and address.endswith(f"/events/{event.id}")

    copy = share.locator(".share-copy")
    expect(copy).to_be_visible()
    copy.click()
    expect(copy).to_contain_text("Link copied")
    expect(share.locator(".share-status")).to_have_text("Link copied to clipboard")
    assert page.evaluate("window.__copied") == [address]
    # The label goes back after a moment, ready for another copy.
    expect(copy).to_contain_text("Copy link", timeout=5000)


def test_share_button_uses_the_device_share_sheet(open_page, site):
    event = site["events"][0]
    page = open_page(init_scripts=[_FAKE_SHARE_SHEET])
    page.goto(f"/events/{event.id}")
    native = page.locator(".share-native")
    expect(native).to_be_visible()
    native.click()
    page.wait_for_function("window.__shared.length === 1")
    shared = page.evaluate("window.__shared[0]")
    assert shared["url"] == page.locator(".event-share").get_attribute("data-share-url")
    assert shared["title"] == event.title


def test_share_button_is_hidden_where_the_browser_cannot_share(open_page, site):
    page = open_page(init_scripts=[_NO_SHARE_SHEET])
    page.goto(f"/events/{site['events'][0].id}")
    expect(page.locator(".share-copy")).to_be_visible()
    expect(page.locator(".share-native")).to_be_hidden()
    # The platform links are plain links and always there.
    expect(page.locator(".event-share a.share-pill").first).to_be_visible()


def test_sign_in_round_trip(open_page, site, monkeypatch, live_server):
    """A guest signs in with a (fake) Google and comes back to the page they
    were on, signed in. Exercises the real cookies and redirects in the
    browser; only the provider itself is a stand-in."""
    from app.routers import auth as auth_router
    from app.services import oauth_login
    from app.services.oauth import ExternalIdentityInfo, MockProvider

    class ComesStraightBack(MockProvider):
        def authorization_url(self, *, state, nonce, redirect_uri):
            super().authorization_url(state=state, nonce=nonce, redirect_uri=redirect_uri)
            # The callback address is built from the configured public URL;
            # send the browser to the same path on the test server.
            callback = live_server + urlparse(redirect_uri).path
            return f"{callback}?code=browser-test&state={state}"

    info = ExternalIdentityInfo(
        provider="google", subject="test:member@example.com", email="member@example.com",
        email_verified=True, display_name=None, avatar_url=None,
    )
    monkeypatch.setattr(auth_router, "enabled_providers", lambda settings: ["google"])
    monkeypatch.setattr(oauth_login, "is_enabled", lambda settings, name: True)
    monkeypatch.setattr(
        oauth_login, "build_provider", lambda settings, name: ComesStraightBack(info)
    )

    page = open_page()
    event = site["events"][0]
    page.goto(f"/events/{event.id}")
    page.locator(".site-nav").get_by_role("link", name="Sign in").click()
    page.wait_for_url(re.compile(r"/auth/login\?next="))
    button = page.get_by_role("link", name="Continue with Google")
    href = button.get_attribute("href")
    assert urlparse(href).path == "/auth/oauth/google"
    assert parse_qs(urlparse(href).query)["next"] == [f"/events/{event.id}"]
    button.click()
    page.wait_for_url(f"**/events/{event.id}")
    expect(page.locator("details.account-menu")).to_be_visible()


# ---------------------------------------------------------------- signed in


def test_account_menu_opens_and_closes(open_page, site):
    page = open_page("member")
    page.goto("/city/ann-arbor")
    menu = page.locator("details.account-menu")
    summary = menu.locator("summary")
    saved_link = menu.get_by_role("link", name="Saved")

    summary.click()
    expect(menu).to_have_attribute("open", "")
    expect(saved_link).to_be_visible()

    # Escape closes it and puts focus back on the button.
    page.keyboard.press("Escape")
    expect(menu).not_to_have_attribute("open", "")
    expect(summary).to_be_focused()

    # So does a click anywhere else.
    summary.click()
    expect(menu).to_have_attribute("open", "")
    page.locator("#results").click()
    expect(menu).not_to_have_attribute("open", "")

    # Its links go where they say.
    summary.click()
    saved_link.click()
    page.wait_for_url("**/account/saved")
    saved = page.get_by_role("link", name="Lecture on Great Lakes Transit", exact=True)
    expect(saved).to_be_visible()


def test_save_unsave_and_follow(open_page, site):
    event = site["events"][0]
    page = open_page("member")
    page.goto(f"/events/{event.id}")

    page.get_by_role("button", name="Save event").click()
    page.wait_for_url(f"**/events/{event.id}")
    unsave = page.get_by_role("button", name="Saved. Unsave this event")
    expect(unsave).to_be_visible()
    page.goto("/account/saved")
    expect(page.get_by_role("link", name=event.title, exact=True)).to_be_visible()

    page.goto(f"/events/{event.id}")
    page.get_by_role("button", name="Saved. Unsave this event").click()
    expect(page.get_by_role("button", name="Save event")).to_be_visible()

    page.get_by_role("button", name="Follow Ann Arbor").click()
    expect(page.get_by_role("button", name="Following Ann Arbor. Unfollow")).to_be_visible()


def test_log_out_signs_the_member_out(open_page, site):
    page = open_page("member")
    page.goto("/account")
    page.locator(".site-nav").get_by_role("button", name="Log Out", exact=True).click()
    expect(page.locator(".site-nav").get_by_role("link", name="Sign in")).to_be_visible()
    expect(page.locator("details.account-menu")).to_have_count(0)
    page.goto("/account")
    expect(page).to_have_url(re.compile(r"/auth/login"))


def test_suggest_a_town_form(open_page, site, db_session):
    page = open_page("member")
    page.goto("/suggest?kind=town")
    form = page.locator("form.suggest-form")
    title = form.locator("#title")

    # The browser refuses an empty required field before anything is sent.
    form.get_by_role("button").last.click()
    expect(page).to_have_url(re.compile(r"/suggest\?kind=town$"))
    assert page.evaluate("document.getElementById('title').validity.valueMissing")

    title.fill("Bloomington")
    form.locator("#region").fill("Indiana")
    form.locator("#university").fill("Indiana University")
    form.get_by_role("button").last.click()
    expect(page.locator(".flash")).to_contain_text("We'll look into adding Bloomington")


def test_feedback_form(open_page, site):
    page = open_page("member")
    page.goto("/feedback")
    form = page.locator("form.suggest-form")
    form.locator("#title").fill("Map does not load")
    form.locator("#details").fill("On the town page the map stays grey.")
    form.get_by_role("button").last.click()
    expect(page).to_have_url(re.compile(r"/feedback"))
    expect(page.locator(".flash")).to_be_visible()


# ---------------------------------------------------------------- admin


def _admin_menu(page, name):
    group = page.locator(".admin-nav-group").filter(
        has=page.get_by_role("button", name=name, exact=True)
    )
    return group, group.locator(".admin-nav-trigger")


def test_admin_menus_open_one_at_a_time(open_page, site, screen):
    page = open_page("admin")
    page.goto("/admin")
    toggle = page.locator(".admin-nav-toggle")
    if screen == "phone":
        # On a phone the sections hide behind one Menu button.
        expect(toggle).to_be_visible()
        expect(page.locator("#admin-nav-sections")).to_be_hidden()
        toggle.click()
        expect(toggle).to_have_attribute("aria-expanded", "true")
        expect(page.locator("#admin-nav-sections")).to_be_visible()
    else:
        expect(toggle).to_be_hidden()

    sources, sources_button = _admin_menu(page, "Sources")
    events, events_button = _admin_menu(page, "Events")

    sources_button.click()
    expect(sources).to_have_class(re.compile(r"\bis-open\b"))
    expect(sources_button).to_have_attribute("aria-expanded", "true")
    expect(sources.locator(".admin-nav-menu")).to_be_visible()

    events_button.click()
    expect(events).to_have_class(re.compile(r"\bis-open\b"))
    expect(sources).not_to_have_class(re.compile(r"\bis-open\b"))
    expect(sources_button).to_have_attribute("aria-expanded", "false")

    page.keyboard.press("Escape")
    expect(events).not_to_have_class(re.compile(r"\bis-open\b"))
    expect(events_button).to_be_focused()

    events_button.click()
    expect(events).to_have_class(re.compile(r"\bis-open\b"))
    # A click on the page below the header, well clear of the open menu.
    page.mouse.click(5, VIEWPORTS[screen][1] - 5)
    expect(events).not_to_have_class(re.compile(r"\bis-open\b"))

    # A link in a menu goes to its page.
    sources_button.click()
    sources.get_by_role("link").first.click()
    page.wait_for_load_state()
    assert "/admin/" in page.url


def test_admin_confirmation_can_be_declined(open_page, site, db_session):
    town = site["town"]
    page = open_page("admin")
    page.goto(f"/admin/cities/{town.id}")
    deactivate = page.get_by_role("button", name=re.compile(r"^Deactivate"))

    # Declining the confirmation sends nothing.
    asked: list[str] = []

    def decline(dialog):
        asked.append(dialog.message)
        dialog.dismiss()

    page.once("dialog", decline)
    deactivate.click()
    page.wait_for_timeout(300)  # a submit, had there been one, would navigate
    assert asked and asked[0].startswith(f"Deactivate {town.name}?")
    db_session.expire_all()
    assert db_session.get(City, town.id).is_active

    # Accepting it goes ahead.
    page.once("dialog", lambda dialog: dialog.accept())
    deactivate.click()
    expect(page.get_by_role("button", name=re.compile(r"^Activate"))).to_be_visible()
    db_session.expire_all()
    assert not db_session.get(City, town.id).is_active


def test_admin_times_show_in_the_viewers_time_zone(open_page, site):
    page = open_page("admin", timezone_id="America/Los_Angeles")
    page.goto("/admin/websites")
    stamp = page.locator("time[data-local-time]").first
    expect(stamp).to_have_attribute("data-localized", "1")
    # Rewritten into Pacific time, with the server's wording kept as a tooltip.
    expect(stamp).to_have_text(re.compile(r"at \d{1,2}:\d{2} [AP]M P[SD]T$"))
    assert stamp.get_attribute("title")
