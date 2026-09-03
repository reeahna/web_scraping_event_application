# Event scraping app — web + scheduler in one container.
#
# Python 3.13 (the project's required floor) plus Chromium and its system
# libraries for the restricted headless-browser fetch strategy. Runs as root so
# the mounted persistent disk (the SQLite database) is writable — standard for a
# single-container Render Docker service.
FROM python:3.13-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# Build tools for wheels that need compiling (e.g. bcrypt); curl for healthchecks.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential curl \
    && rm -rf /var/lib/apt/lists/*

# 1) Python dependencies first, so this layer is cached across code changes.
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

# 2) Chromium + all the OS libraries Playwright needs. Done after pip so the
#    playwright CLI is available; --with-deps installs the apt packages too.
RUN playwright install --with-deps chromium

# 3) Application code.
COPY . .

RUN chmod +x ./start.sh

# Render provides $PORT at runtime; this is just documentation.
ENV PORT=8100
EXPOSE 8100

CMD ["./start.sh"]
