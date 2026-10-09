#!/usr/bin/env bash
# Container entrypoint: run migrations, then the web server (primary) with the
# scheduler supervised alongside it. Both share the one SQLite database on the
# persistent disk.
set -euo pipefail

# Apply any pending database migrations (idempotent; a no-op once up to date).
alembic upgrade head

# Supervise the background scraper: if it exits for any reason, log it and
# restart after a short backoff. Crucially, its death never takes down the web
# server — the site stays up even if the scheduler is unhealthy.
(
  while true; do
    python -m app.scheduler || echo "[start.sh] scheduler exited (code $?); restarting in 5s"
    sleep 5
  done
) &

# The web server is the container's primary process. The container lives and
# dies with it, so the platform restarts the container only when the web server
# itself exits — not on a scheduler blip.
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8100}"
