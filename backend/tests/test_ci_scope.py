"""The CI job selector (.github/scripts/ci_scope.sh classify).

CI skips jobs a change cannot affect. These cases pin down that it only ever
skips what is safe: CI, dependency and test-harness changes run everything,
unknown files run at least test and PostgreSQL, and only docs run nothing.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / ".github" / "scripts" / "ci_scope.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or not SCRIPT.exists(), reason="needs bash and the CI script"
)

ALL = {"test": True, "postgres": True, "browsers": True}
NONE = {"test": False, "postgres": False, "browsers": False}
BACKEND = {"test": True, "postgres": True, "browsers": False}
FRONTEND = {"test": True, "postgres": False, "browsers": True}
TEXT_ONLY = {"test": True, "postgres": False, "browsers": False}


def classify(*paths: str) -> dict[str, bool]:
    result = subprocess.run(
        ["bash", str(SCRIPT), "classify"],
        input="".join(f"{p}\n" for p in paths),
        capture_output=True,
        text=True,
        check=True,
    )
    pairs = dict(line.split("=", 1) for line in result.stdout.split())
    return {key: pairs[key] == "true" for key in ("test", "postgres", "browsers")}


@pytest.mark.parametrize(
    ("paths", "expected"),
    [
        # Docs only: nothing runs.
        (["docs/scheduler.md"], NONE),
        (["README.md", "backend/CLAUDE.md", "frontend/CLAUDE.md"], NONE),
        # CI itself, dependencies and the test harness: everything runs.
        ([".github/workflows/ci.yml"], ALL),
        ([".github/scripts/ci_scope.sh"], ALL),
        (["backend/pyproject.toml"], ALL),
        (["backend/requirements.txt"], ALL),
        (["backend/Dockerfile"], ALL),
        (["backend/alembic.ini"], ALL),
        (["backend/tests/conftest.py"], ALL),
        (["docs/x.md", "backend/requirements.txt"], ALL),
        # Backend code: SQLite and PostgreSQL, no extra browsers.
        (["backend/app/extraction/livewhale.py"], BACKEND),
        (["backend/app/models/event.py"], BACKEND),
        (["backend/migrations/versions/0042_x.py"], BACKEND),
        (["backend/data/college_towns/towns.csv"], BACKEND),
        (["backend/tests/test_public_ui.py"], BACKEND),
        # Unknown files run more, never less.
        (["render.yaml"], BACKEND),
        # Code that builds pages also runs the three browsers.
        (["backend/app/routers/public.py"], ALL),
        (["backend/app/core/templating.py"], ALL),
        (["backend/app/main.py"], ALL),
        (["backend/tests/test_responsive_layout.py"], ALL),
        # Templates, CSS and JS: browsers, no PostgreSQL.
        (["frontend/templates/base.html"], FRONTEND),
        (["frontend/static/css/site.css", "docs/notes.md"], FRONTEND),
        # A changelog line is app content (What's new), not docs, but no
        # layout check opens it.
        (["CHANGELOG.md"], TEXT_ONLY),
        (["CHANGELOG.md", "frontend/static/js/app.js"], FRONTEND),
        (["CHANGELOG.md", "backend/app/services/changelog.py"], BACKEND),
        # Nothing listed is suspicious: everything runs.
        ([], ALL),
    ],
)
def test_classify(paths, expected):
    assert classify(*paths) == expected
