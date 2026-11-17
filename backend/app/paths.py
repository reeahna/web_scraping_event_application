"""Where the frontend lives on disk.

The HTML templates and static assets sit in the repository's top-level
`frontend/` folder, a sibling of `backend/`. The Docker image keeps the same
layout (`/srv/backend` and `/srv/frontend`), so this one relative path works
locally, in tests and in production.
"""

from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BACKEND_DIR.parent / "frontend"
TEMPLATES_DIR = FRONTEND_DIR / "templates"
STATIC_DIR = FRONTEND_DIR / "static"
# The release notes the version history pages are built from. The Docker image
# copies it to /srv/CHANGELOG.md, the same place relative to backend/.
CHANGELOG_PATH = BACKEND_DIR.parent / "CHANGELOG.md"
