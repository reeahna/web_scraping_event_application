#!/usr/bin/env bash
# Container entrypoint: run migrations, then the scheduler and web server
# together. Both share the one SQLite database on the persistent disk.
set -euo pipefail

# Apply any pending database migrations (idempotent; a no-op once up to date).
alembic upgrade head

# Durable background scraper. It is deliberately a separate process from the web
# server (see app/scheduler); here they live in one container so they can share
# the same SQLite file. Per-site timing and locks live in the database.
python -m app.scheduler &
SCHEDULER_PID=$!

# Web server. Bind to the port Render assigns ($PORT), falling back for local runs.
uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8100}" &
WEB_PID=$!

# If either process exits, stop the other and exit non-zero so the platform
# restarts the whole container — keeping web and scheduler in lockstep rather
# than silently running with one of them dead.
wait -n
echo "A process exited; shutting down the container so it is restarted."
kill "$SCHEDULER_PID" "$WEB_PID" 2>/dev/null || true
exit 1
