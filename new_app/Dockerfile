# Production image for the city-events application (Phase 18).
# Builds one image used for every process (web, scheduler, worker, browser
# worker); the command chosen at run time decides the role. Building the image
# is not a deployment — see docs/deployment.md.
FROM python:3.13-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# System deps for psycopg (PostgreSQL) and Playwright's chromium.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libpq-dev curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install ".[dev]" "psycopg[binary]>=3.1"

# Browser worker only: install chromium. Harmless for other roles.
RUN python -m playwright install --with-deps chromium || true

COPY . .

# Runs as an unprivileged user.
RUN useradd -m appuser && chown -R appuser /app
USER appuser

EXPOSE 8000

# Default role is the web server; override `command` for other roles
# (see docker-compose.prod.yml / docs/deployment.md).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
