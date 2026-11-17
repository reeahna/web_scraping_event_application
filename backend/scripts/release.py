"""Cut a release: turn CHANGELOG.md's Unreleased notes into the next version.

Run from backend/:

    python scripts/release.py minor                 # new features: 1.9.0 -> 1.10.0
    python scripts/release.py patch                 # fixes only:   1.9.0 -> 1.9.1
    python scripts/release.py major                 # big change:   1.9.0 -> 2.0.0
    python scripts/release.py minor --name "Dark mode"

It renames `## [Unreleased]` to `## [x.y.z] - <today> - <name>`, opens a fresh
empty Unreleased section above it, and sets the same version in pyproject.toml.
Commit both files. Nothing is pushed or tagged.
"""

import argparse
import re
import sys
from datetime import date
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from app.services.changelog import parse_changelog  # noqa: E402

CHANGELOG = BACKEND_DIR.parent / "CHANGELOG.md"
PYPROJECT = BACKEND_DIR / "pyproject.toml"
UNRELEASED_HEADING = "## [Unreleased]"


def next_version(current: str, part: str) -> str:
    major, minor, patch = (int(n) for n in current.split("."))
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def cut_release(text: str, part: str, name: str | None, today: date) -> tuple[str, str]:
    """Return (new changelog text, new version). Raises SystemExit on a problem."""
    releases = parse_changelog(text)
    unreleased = next((r for r in releases if r.is_unreleased), None)
    if unreleased is None or not unreleased.has_notes:
        raise SystemExit("Nothing under ## [Unreleased] to release.")
    current = next(r.version for r in releases if not r.is_unreleased)
    version = next_version(current, part)
    heading = f"## [{version}] - {today.isoformat()}" + (f" - {name}" if name else "")
    new_text = text.replace(UNRELEASED_HEADING, f"{UNRELEASED_HEADING}\n\n{heading}", 1)
    return new_text, version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("part", choices=["major", "minor", "patch"])
    parser.add_argument("--name", help="a short name for the release, e.g. 'Dark mode'")
    args = parser.parse_args(argv)

    new_text, version = cut_release(
        CHANGELOG.read_text(encoding="utf-8"), args.part, args.name, date.today()
    )
    CHANGELOG.write_text(new_text, encoding="utf-8")

    pyproject = PYPROJECT.read_text(encoding="utf-8")
    pyproject = re.sub(r'(?m)^version = "[^"]+"', f'version = "{version}"', pyproject, count=1)
    PYPROJECT.write_text(pyproject, encoding="utf-8")

    print(f"Released {version}. Commit CHANGELOG.md and backend/pyproject.toml.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
