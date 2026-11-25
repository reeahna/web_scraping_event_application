"""Shared setup for the tests that drive real browsers.

Used by tests/test_responsive_layout.py (does every page fit the screen) and
tests/test_browser_interactions.py (do the menus, forms and buttons work).
The `live_server` fixture itself lives in tests/conftest.py.

Environment:
  LAYOUT_BROWSERS="chromium,firefox,webkit"  engines to run (default chromium).
      CI runs one engine per job. An engine that is not installed is skipped.
  PLAYWRIGHT_CHROMIUM_EXECUTABLE=<path>  a Chromium binary to use instead of
      the one Playwright downloads, for machines where `playwright install`
      cannot run or installed a different build.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

BROWSERS = [
    name.strip()
    for name in os.environ.get("LAYOUT_BROWSERS", "chromium").split(",")
    if name.strip()
]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def running_server(app) -> Iterator[str]:
    """Serve `app` on a free local port in a background thread; yields its URL.

    Same process as the test, so the test's database fixtures and any
    monkeypatching apply to what the browser sees."""
    import uvicorn

    port = free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("live server did not start")
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def launch(pw, name: str):
    """Start a headless browser, or skip the test if that engine is missing."""
    browser_type = getattr(pw, name)
    options = {}
    executable = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
    if name == "chromium" and executable:
        options["executable_path"] = executable
    try:
        return browser_type.launch(**options)
    except Exception as exc:  # not installed in this environment
        pytest.skip(f"{name} is not available: {str(exc).splitlines()[0]}")


def session_cookie(client, login, email: str) -> dict:
    """Sign `email` in through the test client and return its session cookie,
    ready for a browser context's add_cookies (add a "url")."""
    from app.config import get_settings

    client.cookies.clear()
    login(email)
    name = get_settings().session_cookie_name
    return {"name": name, "value": client.cookies.get(name)}
