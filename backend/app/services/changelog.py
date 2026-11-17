"""Version history, read from the repository's CHANGELOG.md.

CHANGELOG.md is the single source of truth for the app's version and its
release notes. Each release is a `## [x.y.z] - YYYY-MM-DD - Optional name`
heading followed by two optional sections:

* `### For everyone`: changes a visitor or signed-in user would notice. These
  are the only notes on the public What's new page.
* `### Admin and behind the scenes`: admin and pipeline changes, shown only in
  the admin version history alongside the public ones.

`## [Unreleased]` collects notes for the next release; only admins see it.
The newest released heading is the app's version (`APP_VERSION`).

The file is parsed once per process. Notes may use **bold**, `code` and
[links](https://...); everything else is escaped before it reaches a template.
"""

import html
import re
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path

from markupsafe import Markup

from app.paths import CHANGELOG_PATH

REPO_URL = "https://github.com/reeahna/web_scraping_event_application"

PUBLIC_SECTION = "For everyone"
ADMIN_SECTION = "Admin and behind the scenes"
SECTIONS = (PUBLIC_SECTION, ADMIN_SECTION)

UNRELEASED = "Unreleased"

_RELEASE_RE = re.compile(
    r"^## \[(?P<version>[^\]]+)\]"
    r"(?: - (?P<date>\d{4}-\d{2}-\d{2}))?"
    r"(?: - (?P<name>.+?))?\s*$"
)
_SECTION_RE = re.compile(r"^### (?P<title>.+?)\s*$")
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
_PR_REF_RE = re.compile(r"#(\d+)")
# A "(#12)" or "(#12, #13)" reference, removed on the public page.
_INLINE_PRS_RE = re.compile(r"\s*\((?:#\d+(?:,\s*)?)+\)")


@dataclass(frozen=True)
class Release:
    version: str
    released_on: date | None
    name: str | None
    notes: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def is_unreleased(self) -> bool:
        return self.version == UNRELEASED

    @property
    def public_notes(self) -> tuple[str, ...]:
        return self.notes.get(PUBLIC_SECTION, ())

    @property
    def admin_notes(self) -> tuple[str, ...]:
        return self.notes.get(ADMIN_SECTION, ())

    @property
    def has_notes(self) -> bool:
        return any(self.notes.values())


class ChangelogError(ValueError):
    """CHANGELOG.md does not follow the format the app reads."""


def parse_changelog(text: str) -> list[Release]:
    """Parse changelog text into releases, in file order (newest first).

    Text before the first `## [` heading is the file's own instructions and is
    skipped, as are sections other than the two known ones.
    """
    releases: list[Release] = []
    current: dict | None = None
    section: str | None = None

    def finish() -> None:
        if current is not None:
            releases.append(
                Release(
                    version=current["version"],
                    released_on=current["date"],
                    name=current["name"],
                    notes={k: tuple(v) for k, v in current["notes"].items() if v},
                )
            )

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip()
        if line.startswith("## "):
            match = _RELEASE_RE.match(line)
            if not match:
                if current is None:
                    continue  # a heading in the preamble, e.g. "How to keep it current"
                raise ChangelogError(f"line {lineno}: unreadable release heading {line!r}")
            finish()
            version = match["version"]
            if version != UNRELEASED and not _SEMVER_RE.match(version):
                raise ChangelogError(f"line {lineno}: {version!r} is not x.y.z")
            if version != UNRELEASED and not match["date"]:
                raise ChangelogError(f"line {lineno}: release {version} has no date")
            current = {
                "version": version,
                "date": date.fromisoformat(match["date"]) if match["date"] else None,
                "name": match["name"],
                "notes": {title: [] for title in SECTIONS},
            }
            section = None
            continue
        if current is None:
            continue
        section_match = _SECTION_RE.match(line)
        if section_match:
            title = section_match["title"]
            section = title if title in SECTIONS else None
            continue
        if section is None:
            continue
        notes = current["notes"][section]
        if line.startswith("- "):
            notes.append(line[2:].strip())
        elif line.startswith("  ") and line.strip() and notes:
            notes[-1] = f"{notes[-1]} {line.strip()}"
    finish()
    return releases


def render_note(note: str, *, public: bool) -> Markup:
    """Turn one note into safe HTML.

    The public page drops PR references; the admin page links them to GitHub.
    """
    if public:
        note = _INLINE_PRS_RE.sub("", note)
    out = html.escape(note, quote=True)
    # `code` first, so ** inside code stays literal.
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)

    def link(match: re.Match) -> str:
        label, url = match[1], html.unescape(match[2])
        if not (url.startswith("https://") or url.startswith("/")):
            return match[0]
        return f'<a href="{html.escape(url, quote=True)}">{label}</a>'

    out = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", link, out)
    if not public:
        out = _PR_REF_RE.sub(
            lambda m: f'<a href="{REPO_URL}/pull/{m[1]}">#{m[1]}</a>', out
        )
    return Markup(out)


@lru_cache(maxsize=1)
def load_changelog(path: Path = CHANGELOG_PATH) -> tuple[Release, ...]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ()
    return tuple(parse_changelog(text))


def released(releases: tuple[Release, ...] | None = None) -> list[Release]:
    releases = load_changelog() if releases is None else releases
    return [r for r in releases if not r.is_unreleased]


def public_releases() -> list[Release]:
    """Releases with something for everyone, for the What's new page."""
    return [r for r in released() if r.public_notes]


def current_version() -> str:
    """The newest released version, or "0.0.0" if the changelog is missing."""
    found = released()
    return found[0].version if found else "0.0.0"


APP_VERSION = current_version()
