"""Restricted Playwright fetch strategy.

Playwright here is a *fetch strategy*, not a scraper: it renders a page that
ordinary HTTP cannot, captures the rendered HTML and any JSON the page fetched,
and hands both back so the ordinary PatternRegistry detection/extraction runs
against them. It executes only the closed action plan (app.schemas.browser) —
never arbitrary JavaScript.

Safety, all enforced here rather than trusted to the caller:

* the initial URL is SSRF-validated before the browser launches, and every
  subrequest is re-validated by a route interceptor — a request to a private,
  loopback, or non-http(s) target is aborted, so a rendered page cannot be
  used to reach internal services
* downloads, popups/new windows, and (where supported) service workers are
  disabled; media is blocked by default to keep renders cheap
* a challenge/login wall (Cloudflare, CAPTCHA, "sign in to continue") is
  detected and reported as blocked — never solved, worked around, or
  submitted to
* the browser, context and pages are always closed in a finally block, even
  on error or timeout

The SSRF host check is indirected through `_host_allowed` so tests can permit
a local fixture server without weakening the production default.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from app.extraction.network_safety import (
    BlockedHostError,
    hostname_and_port,
    resolve_and_validate_host,
    validate_request_url,
)
from app.schemas.browser import BrowserPlan

_CHALLENGE_MARKERS = (
    "cloudflare",
    "access denied",
    "are you a robot",
    "captcha",
    "verify you are human",
    "checking your browser",
    "enable javascript and cookies",
)
_LOGIN_WALL_MARKERS = ("sign in to continue", "please log in", "log in to view", "members only")
_ALLOWED_SCHEMES = frozenset({"http", "https", "data", "about", "blob"})
_BLOCKED_RESOURCE_TYPES = frozenset({"image", "media", "font"})


# Bound on a captured request body — enough to template a filter, never a
# whole upload.
_MAX_REQUEST_BODY_CHARS = 4_000

# Request headers safe to retain from an observed request. Everything else
# (Cookie, Authorization, sec-ch-*, cache/transport headers) is dropped before
# it ever reaches observed_requests. Kept deliberately narrow; request_recipe's
# capture applies the same allowlist again when it builds the stored recipe.
_SAFE_REQUEST_HEADERS = frozenset(
    {"accept", "referer", "accept-language", "content-type", "x-requested-with"}
)


@dataclass
class BrowserRenderResult:
    final_url: str
    rendered_html: str
    status_code: int
    observed_json: list[tuple[str, object]] = field(default_factory=list)
    # url -> bounded, redacted request/response metadata for JSON responses, for
    # safe recurring HTTP replay. Never cookies, auth, tokens, or arbitrary
    # headers; only the method, request content-type, a bounded request body,
    # safe query-param names, and the response status/content-type.
    observed_requests: dict[str, dict] = field(default_factory=dict)
    blocked_reason: str | None = None
    warnings: tuple[str, ...] = ()


async def _host_allowed(url: str) -> bool:
    """True if `url` is a safe public http(s) target. Indirected so tests can
    permit a local fixture server; production behaviour is the ordinary
    SSRF-safe check."""
    try:
        safe = validate_request_url(url)
        host, port = hostname_and_port(safe)
        await resolve_and_validate_host(host, port)
    except BlockedHostError:
        return False
    return True


def _challenge_reason(html: str, status: int) -> str | None:
    if status in (401, 403, 429):
        return f"http_{status}"
    lowered = html[:6000].lower()
    for marker in _CHALLENGE_MARKERS:
        if marker in lowered:
            return f"challenge_marker:{marker}"
    for marker in _LOGIN_WALL_MARKERS:
        if marker in lowered:
            return f"login_wall:{marker}"
    return None


class BrowserFetchStrategy:
    """`_launcher` is a testability hook only: production passes None and the
    real Playwright chromium is used. There is no mock browser — tests drive
    the real headless browser against a local fixture server."""

    def __init__(self, launcher=None) -> None:
        self._launcher = launcher

    async def render(self, url: str, plan: BrowserPlan | None = None) -> BrowserRenderResult:
        from app.schemas.browser import default_plan

        plan = plan or default_plan()

        if not await _host_allowed(url):
            return BrowserRenderResult(
                final_url=url, rendered_html="", status_code=0,
                blocked_reason=f"ssrf_blocked:{urlsplit(url).hostname}",
            )

        from playwright.async_api import async_playwright

        observed_json: list[tuple[str, object]] = []
        observed_requests: dict[str, dict] = {}
        warnings: list[str] = []

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(
                accept_downloads=False,
                service_workers="block",
                java_script_enabled=True,
            )
            context.set_default_timeout(plan.max_total_ms)
            try:
                page = await context.new_page()
                # New windows/popups are closed immediately rather than
                # followed.
                context.on("page", lambda p: _safe_close(p))
                await self._install_guards(
                    context, plan, observed_json, observed_requests, warnings
                )

                response = await page.goto(url, wait_until="domcontentloaded")
                status = response.status if response is not None else 0
                final_url = page.url

                html = await page.content()
                challenge = _challenge_reason(html, status)
                if challenge is not None:
                    # Never attempt to solve or bypass — report and stop.
                    return BrowserRenderResult(
                        final_url=final_url, rendered_html="", status_code=status,
                        blocked_reason=challenge, warnings=tuple(warnings),
                    )

                await self._run_plan(page, plan, warnings)
                html = await page.content()
                return BrowserRenderResult(
                    final_url=page.url,
                    rendered_html=html,
                    status_code=status,
                    observed_json=observed_json,
                    observed_requests=observed_requests,
                    warnings=tuple(warnings),
                )
            except Exception as exc:  # noqa: BLE001 - a render failure must not crash the app
                return BrowserRenderResult(
                    final_url=url, rendered_html="", status_code=0,
                    blocked_reason=f"browser_error:{type(exc).__name__}",
                    warnings=tuple(warnings),
                )
            finally:
                # Always tear down, even on timeout/error.
                await _safe_close_context(context)
                await _safe_close_browser(browser)

    async def _install_guards(
        self, context, plan, observed_json, observed_requests, warnings
    ) -> None:
        allowed_hosts: dict[str, bool] = {}

        async def route_handler(route):
            request = route.request
            parsed = urlsplit(request.url)
            scheme = (parsed.scheme or "").lower()
            if scheme not in _ALLOWED_SCHEMES:
                await route.abort()
                return
            if scheme in ("data", "about", "blob"):
                await route.continue_()
                return
            if plan.block_media and request.resource_type in _BLOCKED_RESOURCE_TYPES:
                await route.abort()
                return
            host = parsed.hostname or ""
            if host not in allowed_hosts:
                allowed_hosts[host] = await _host_allowed(request.url)
            if not allowed_hosts[host]:
                warnings.append(f"blocked_subrequest:{host}")
                await route.abort()
                return
            await route.continue_()

        await context.route("**/*", route_handler)

        if plan.capture_json:
            async def on_response(response):
                ctype = (response.headers or {}).get("content-type", "")
                if "json" not in ctype.lower():
                    return
                try:
                    payload = await response.json()
                except Exception:  # noqa: BLE001 - a non-JSON/oversized body is skipped
                    return
                if len(observed_json) < 20:
                    observed_json.append((response.url, payload))
                    _capture_request_meta(response, ctype, observed_requests)

            context.on("response", lambda r: _fire(on_response(r)))

    async def _run_plan(self, page, plan: BrowserPlan, warnings: list[str]) -> None:
        for action in plan.actions:
            try:
                await self._run_action(page, action)
            except Exception as exc:  # noqa: BLE001 - one action failing is not fatal
                warnings.append(f"action_failed:{action.action}:{type(exc).__name__}")

    async def _run_action(self, page, action) -> None:
        kind = action.action
        if kind == "wait_for_selector":
            await page.wait_for_selector(action.selector, timeout=action.timeout_ms)
        elif kind == "network_idle":
            await page.wait_for_load_state("networkidle", timeout=action.timeout_ms)
        elif kind == "click":
            await page.click(action.selector, timeout=action.timeout_ms)
        elif kind == "load_more":
            for _ in range(action.max_clicks):
                button = await page.query_selector(action.selector)
                if button is None:
                    break
                await button.click()
                await page.wait_for_timeout(action.settle_ms)
        elif kind == "scroll":
            for _ in range(action.max_scrolls):
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(action.delay_ms)
        elif kind == "dismiss_banner":
            for selector in action.selectors:
                element = await page.query_selector(selector)
                if element is not None:
                    await element.click()
                    break


def _capture_request_meta(response, response_content_type: str, observed_requests: dict) -> None:
    """Bounded, redacted request/response metadata for one JSON response — for
    safe recurring HTTP replay. Records only the method, the request
    content-type, a bounded request body, safe query-param NAMES (never values,
    which may carry tokens), and the response status/content-type. Never
    cookies, auth headers, tokens, or arbitrary request headers."""
    try:
        request = response.request
        req_headers = request.headers or {}
        body = request.post_data
        if isinstance(body, str) and len(body) > _MAX_REQUEST_BODY_CHARS:
            body = None  # oversized bodies are dropped, not truncated-and-trusted
        query_names = sorted(
            {p.split("=", 1)[0] for p in urlsplit(request.url).query.split("&") if p}
        )
        # Pre-filter headers to the safe allowlist HERE so cookies, Authorization,
        # sec-ch-ua, etc. never enter observed_requests at all. request_recipe's
        # capture re-filters, but keeping them out of memory is defence in depth.
        safe_headers = {
            name: value
            for name, value in req_headers.items()
            if name.lower() in _SAFE_REQUEST_HEADERS
        }
        observed_requests[response.url] = {
            "method": request.method,
            # Full URL (with query) so a recipe can preserve the required nested
            # `json`/`token` params. Values may include a *public* site token;
            # this dict is transient and the token is redacted in UI/logs.
            "request_url": request.url,
            "request_content_type": req_headers.get("content-type"),
            "request_body": body,
            "request_headers": safe_headers,
            "query_param_names": query_names,
            "response_status": response.status,
            "response_content_type": response_content_type,
        }
    except Exception:  # noqa: BLE001 - metadata capture must never break a render
        return


def _fire(coro) -> None:
    import asyncio

    asyncio.ensure_future(coro)  # noqa: RUF006 - fire-and-forget response observer


def _safe_close(page) -> None:
    import asyncio

    asyncio.ensure_future(page.close())  # noqa: RUF006


async def _safe_close_context(context) -> None:
    with contextlib.suppress(Exception):
        await context.close()


async def _safe_close_browser(browser) -> None:
    with contextlib.suppress(Exception):
        await browser.close()


def observed_json_as_text(observed_json: list[tuple[str, object]]) -> list[str]:
    """Serializes observed JSON payloads back to text so the ordinary
    JSON-in-script / API detectors can run against them."""
    out: list[str] = []
    for _url, payload in observed_json:
        try:
            out.append(json.dumps(payload))
        except (TypeError, ValueError):
            continue
    return out
