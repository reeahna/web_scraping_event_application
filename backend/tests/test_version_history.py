"""Version history: CHANGELOG.md, the What's new page and the admin page."""

import importlib.util
import re
import tomllib
from datetime import date

import pytest

from app.core.permissions import ADMINISTRATOR
from app.paths import BACKEND_DIR, CHANGELOG_PATH
from app.services.changelog import (
    APP_VERSION,
    PUBLIC_SECTION,
    ChangelogError,
    load_changelog,
    parse_changelog,
    render_note,
)

SAMPLE = """# Changelog

Preamble that is not a release.

## How to keep it current
- not a note

## [Unreleased]

### Admin and behind the scenes
- Coming soon for admins. (#70)

## [1.1.0] - 2026-11-20 - Dark mode

### For everyone
- A **dark** theme, see [docs](https://example.com/docs). (#68)
- A long note that
  wraps onto a second line. (#69, #71)

### Admin and behind the scenes
- `scheduler` tweak. (#67)

## [1.0.0] - 2026-09-03

### Admin and behind the scenes
- Only admin notes here.
"""


# --- The real CHANGELOG.md --------------------------------------------------


def test_changelog_parses_and_is_newest_first():
    releases = [r for r in load_changelog() if not r.is_unreleased]
    assert releases, "CHANGELOG.md has no releases"
    dates = [r.released_on for r in releases]
    assert dates == sorted(dates, reverse=True)
    versions = [tuple(int(n) for n in r.version.split(".")) for r in releases]
    assert versions == sorted(versions, reverse=True)
    assert len(set(versions)) == len(versions)


def test_app_version_is_newest_release_and_matches_pyproject():
    newest = next(r for r in load_changelog() if not r.is_unreleased)
    assert newest.version == APP_VERSION
    pyproject = tomllib.loads((BACKEND_DIR / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["version"] == APP_VERSION, (
        "Bump backend/pyproject.toml with scripts/release.py so it matches CHANGELOG.md"
    )


def test_every_release_has_notes():
    for release in load_changelog():
        if not release.is_unreleased:
            assert release.has_notes, release.version


def test_changelog_is_copied_into_the_docker_image():
    dockerfile = (BACKEND_DIR / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY CHANGELOG.md /srv/CHANGELOG.md" in dockerfile
    assert CHANGELOG_PATH.name == "CHANGELOG.md" and CHANGELOG_PATH.parent == BACKEND_DIR.parent


# --- Parsing and rendering --------------------------------------------------


def test_parse_sample():
    unreleased, v110, v100 = parse_changelog(SAMPLE)
    assert unreleased.is_unreleased and unreleased.admin_notes == ("Coming soon for admins. (#70)",)
    assert v110.version == "1.1.0" and v110.name == "Dark mode"
    assert v110.released_on == date(2026, 11, 20)
    assert v110.public_notes[1] == "A long note that wraps onto a second line. (#69, #71)"
    assert v110.admin_notes == ("`scheduler` tweak. (#67)",)
    assert v100.name is None and v100.public_notes == ()


@pytest.mark.parametrize(
    "heading",
    ["## [1.2] - 2026-01-01", "## [1.2.0]", "## [1.2.0] - Jan 1"],
)
def test_bad_headings_are_refused(heading):
    with pytest.raises(ChangelogError):
        parse_changelog(f"## [Unreleased]\n\n{heading}\n")


def test_public_render_hides_pr_numbers_and_escapes():
    note = "Use <b>this</b> **now** with `x<y` [docs](https://example.com) (#12, #13)."
    out = str(render_note(note, public=True))
    assert "#12" not in out and "#13" not in out
    assert "&lt;b&gt;this&lt;/b&gt;" in out
    assert "<strong>now</strong>" in out and "<code>x&lt;y</code>" in out
    assert '<a href="https://example.com">docs</a>' in out
    assert out.endswith("docs</a>.")


def test_admin_render_links_pr_numbers():
    out = str(render_note("Fix (#64)", public=False))
    url = "https://github.com/reeahna/web_scraping_event_application/pull/64"
    assert f'<a href="{url}">#64</a>' in out


def test_only_safe_link_schemes():
    out = str(render_note("[x](javascript:alert(1))", public=True))
    assert "<a " not in out


# --- Release script ---------------------------------------------------------


def _release_module():
    spec = importlib.util.spec_from_file_location(
        "release_script", BACKEND_DIR / "scripts" / "release.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_release_script_moves_unreleased_into_a_version():
    release = _release_module()
    text, version = release.cut_release(SAMPLE, "minor", "Next", date(2026, 12, 1))
    assert version == "1.2.0"
    releases = parse_changelog(text)
    assert releases[0].is_unreleased and not releases[0].has_notes
    assert releases[1].version == "1.2.0" and releases[1].name == "Next"
    assert releases[1].admin_notes == ("Coming soon for admins. (#70)",)
    assert release.next_version("1.9.3", "patch") == "1.9.4"
    assert release.next_version("1.9.3", "major") == "2.0.0"


def test_release_script_refuses_an_empty_unreleased():
    release = _release_module()
    empty = SAMPLE.replace("- Coming soon for admins. (#70)\n", "")
    with pytest.raises(SystemExit):
        release.cut_release(empty, "patch", None, date(2026, 12, 1))


# --- Pages ------------------------------------------------------------------


def test_whats_new_shows_public_notes_only(client):
    resp = client.get("/whats-new")
    assert resp.status_code == 200
    page = resp.text
    assert "<h1>What's new</h1>" in page
    assert f"version {APP_VERSION}" in page
    assert "Admin and behind the scenes" not in page
    assert "Unreleased" not in page
    # PR numbers are for admins.
    assert not re.search(r"\(#\d+", page)
    for release in load_changelog():
        if release.public_notes and not release.is_unreleased:
            assert f"Version {release.version}" in page
    first_admin_only = next(
        r for r in load_changelog() if r.admin_notes and not r.is_unreleased
    ).admin_notes[0]
    assert str(render_note(first_admin_only, public=True)) not in page


def test_footer_shows_version_and_links_whats_new(client):
    page = client.get("/privacy").text
    assert f">Version {APP_VERSION}</a>" in page
    assert '<a href="/whats-new">What\'s new</a>' in page


def test_admin_version_history_shows_everything(client, make_user, login):
    make_user(email="vh@example.com", password="vh-pass-1234", role_name=ADMINISTRATOR)
    login("vh@example.com", "vh-pass-1234")
    resp = client.get("/admin/version-history")
    assert resp.status_code == 200
    page = resp.text
    assert "<h1>Version history</h1>" in page
    assert "Admin and behind the scenes" in page and PUBLIC_SECTION in page
    assert "/pull/" in page
    assert 'href="/admin/version-history" aria-current="page"' in page
    for release in load_changelog():
        if not release.is_unreleased:
            assert release.version in page


def test_admin_version_history_needs_admin(client, make_user, login):
    resp = client.get("/admin/version-history", follow_redirects=False)
    assert resp.status_code in (302, 303, 401)
    make_user(email="plain@example.com", password="plain-pass-1234")
    login("plain@example.com", "plain-pass-1234")
    assert client.get("/admin/version-history").status_code == 403


def test_whats_new_in_sitemap_and_readiness(client):
    assert "/whats-new</loc>" in client.get("/sitemap.xml").text
    assert client.get("/health/ready").json()["version"] == APP_VERSION
