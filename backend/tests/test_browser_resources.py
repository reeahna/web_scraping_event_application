"""The browser fetch strategy's resource limits on a small production box.

Production runs the web server, the scheduler and Chromium in one 512MB
instance. Scheduled runs, bulk imports and onboarding can all want a browser at
the same moment, so renders share a per-process cap; and a render that ends
while subrequests are still being checked must not leave route callbacks
raising TargetClosedError against the closed context.
"""

from __future__ import annotations

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import AsyncMock, patch

import pytest

from app.config import get_settings
from app.extraction.browser import BrowserFetchStrategy, BrowserRenderResult
from app.schemas.browser import BrowserPlan, ScrollAction

# The subrequests go to a second host name for the same server, because the
# host check runs once per host: a new host is what keeps a request waiting.
_SLOW_SUBREQUESTS_PAGE = """<!DOCTYPE html><html><body><p>ready</p>
<script>
  const other = 'http://localhost:' + location.port;
  for (let i = 0; i < 3; i++) { fetch(other + '/slow-' + i).catch(() => {}); }
</script></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence
        pass

    def do_GET(self):  # noqa: N802
        body = _SLOW_SUBREQUESTS_PAGE if self.path == "/page" else "<html></html>"
        encoded = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


@pytest.fixture(scope="module")
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()


@pytest.fixture(scope="module")
def browser_available():
    async def _check():
        try:
            from playwright.async_api import async_playwright

            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                await browser.close()
            return True
        except Exception:  # noqa: BLE001
            return False

    return asyncio.run(_check())


def _track_concurrency(peak: dict):
    async def fake_impl(self, url, plan):
        peak["now"] += 1
        peak["max"] = max(peak["max"], peak["now"])
        await asyncio.sleep(0.05)
        peak["now"] -= 1
        return BrowserRenderResult(final_url=url, rendered_html="", status_code=200)

    return fake_impl


async def _render_many(count: int) -> None:
    strategy = BrowserFetchStrategy()
    await asyncio.gather(*(strategy.render(f"https://example.com/{i}") for i in range(count)))


def test_concurrent_renders_run_one_browser_at_a_time_by_default():
    peak = {"now": 0, "max": 0}
    with (
        patch("app.extraction.browser._host_allowed", new=AsyncMock(return_value=True)),
        patch.object(BrowserFetchStrategy, "_render_impl", _track_concurrency(peak)),
    ):
        asyncio.run(_render_many(4))
    assert peak["max"] == 1


def test_the_browser_cap_is_configurable(monkeypatch):
    monkeypatch.setattr(get_settings(), "browser_max_concurrent", 2)
    peak = {"now": 0, "max": 0}
    with (
        patch("app.extraction.browser._host_allowed", new=AsyncMock(return_value=True)),
        patch.object(BrowserFetchStrategy, "_render_impl", _track_concurrency(peak)),
    ):
        asyncio.run(_render_many(4))
    assert peak["max"] == 2


def test_subrequests_still_being_checked_at_teardown_do_not_raise(server, browser_available, capfd):
    if not browser_available:
        pytest.skip("chromium is not available in this environment")

    async def slow_host_check(url: str) -> bool:
        # The page itself is allowed at once; its subrequests are still being
        # checked when the render finishes and the browser is torn down.
        if "localhost" in url:
            await asyncio.sleep(0.5)
        return True

    loop_errors: list[dict] = []

    async def run() -> BrowserRenderResult:
        asyncio.get_running_loop().set_exception_handler(
            lambda _loop, ctx: loop_errors.append(ctx)
        )
        with patch("app.extraction.browser._host_allowed", new=slow_host_check):
            result = await BrowserFetchStrategy().render(
                f"{server}/page",
                BrowserPlan(actions=[ScrollAction(max_scrolls=1, delay_ms=200)]),
            )
            # Give the pending host checks time to finish and reach continue_.
            await asyncio.sleep(1.0)
        return result

    result = asyncio.run(run())

    assert result.blocked_reason is None
    assert "ready" in result.rendered_html
    captured = capfd.readouterr()
    output = captured.out + captured.err
    assert "TargetClosedError" not in output, output
    assert not loop_errors, loop_errors
