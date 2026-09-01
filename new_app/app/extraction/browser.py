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

import asyncio
import contextlib
import hashlib
import json
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.extraction.network_safety import (
    BlockedHostError,
    hostname_and_port,
    resolve_and_validate_host,
    validate_request_url,
)
from app.extraction.types import FetchResponse
from app.schemas.browser import BrowserPlan

if TYPE_CHECKING:
    from app.extraction.types import FetchRequest
    from app.schemas.extraction import FetchConfig

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


@dataclass
class BrowserJsonPage:
    """One page fetched in-context via the page's own fetch(): the request URL,
    the HTTP status, and the parsed JSON body (None if it did not parse)."""

    url: str
    status: int
    json: object | None


@dataclass
class BrowserPagedResult:
    final_url: str
    status_code: int
    pages: list[BrowserJsonPage] = field(default_factory=list)
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


def _loop_can_spawn_subprocess() -> bool:
    """Whether the running event loop can spawn subprocesses (Playwright's node
    driver needs one). On Windows only ``ProactorEventLoop`` can; uvicorn run
    with ``--reload`` or ``--workers`` drives the web process on a
    ``SelectorEventLoop``, which raises ``NotImplementedError`` on launch."""
    if sys.platform != "win32":
        return True
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return True
    return isinstance(loop, asyncio.ProactorEventLoop)


async def _run_browser(coro_factory):
    """Run a Playwright coroutine, offloading it to a dedicated thread with its
    own ``ProactorEventLoop`` when the current loop cannot spawn subprocesses
    (the Windows ``SelectorEventLoop`` used by ``uvicorn --reload``). Everywhere
    else — the scheduler process, POSIX — it runs inline on the current loop.
    ``coro_factory`` is a zero-arg callable so the coroutine is created inside
    whichever loop will actually run it."""
    if _loop_can_spawn_subprocess():
        return await coro_factory()

    outcome: dict = {}

    def _worker() -> None:
        worker_loop = asyncio.ProactorEventLoop()
        try:
            asyncio.set_event_loop(worker_loop)
            outcome["value"] = worker_loop.run_until_complete(coro_factory())
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller loop
            outcome["error"] = exc
        finally:
            # Drain the fire-and-forget response observers before closing, so
            # they don't emit "Task was destroyed but it is pending" warnings.
            with contextlib.suppress(Exception):
                pending = asyncio.all_tasks(worker_loop)
                for task in pending:
                    task.cancel()
                if pending:
                    worker_loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
            with contextlib.suppress(Exception):
                asyncio.set_event_loop(None)
            worker_loop.close()

    await asyncio.get_running_loop().run_in_executor(None, _worker)
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


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

        # Browser launch is offloaded to a Proactor-backed thread when the
        # request loop cannot spawn subprocesses (uvicorn --reload on Windows).
        return await _run_browser(lambda: self._render_impl(url, plan))

    async def _render_impl(self, url: str, plan: BrowserPlan) -> BrowserRenderResult:
        from playwright.async_api import async_playwright

        observed_json: list[tuple[str, object]] = []
        observed_requests: dict[str, dict] = {}
        warnings: list[str] = []
        browser = None
        context = None

        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                context = await browser.new_context(
                    accept_downloads=False,
                    service_workers="block",
                    java_script_enabled=True,
                    user_agent=plan.user_agent,
                )
                context.set_default_timeout(plan.max_total_ms)
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
        except Exception as exc:  # noqa: BLE001 - a render/launch failure must not crash the app
            return BrowserRenderResult(
                final_url=url, rendered_html="", status_code=0,
                blocked_reason=f"browser_error:{type(exc).__name__}",
                warnings=tuple(warnings),
            )
        finally:
            # Always tear down, even on timeout/error/launch failure.
            await _safe_close_context(context)
            await _safe_close_browser(browser)

    async def render_and_fetch_json_pages(
        self,
        source_page_url: str,
        plan: BrowserPlan | None = None,
        *,
        next_url,
        max_pages: int,
    ) -> BrowserPagedResult:
        """Navigate the source page ONCE (establishing the browser session the
        edge/bot check requires), then fetch a sequence of JSON pages from within
        that same page context via its own `fetch()` — same origin, same cookies,
        same Referer. `next_url(pages)` decides the next URL from the pages
        collected so far (returning None to stop); the caller owns pagination
        policy, this method owns only the browser mechanics. The context/browser
        are always closed in the finally block."""
        from app.schemas.browser import default_plan

        plan = plan or default_plan()

        if not await _host_allowed(source_page_url):
            return BrowserPagedResult(
                final_url=source_page_url, status_code=0,
                blocked_reason=f"ssrf_blocked:{urlsplit(source_page_url).hostname}",
            )

        return await _run_browser(
            lambda: self._render_and_fetch_json_pages_impl(
                source_page_url, plan, next_url=next_url, max_pages=max_pages
            )
        )

    async def _render_and_fetch_json_pages_impl(
        self,
        source_page_url: str,
        plan: BrowserPlan,
        *,
        next_url,
        max_pages: int,
    ) -> BrowserPagedResult:
        from playwright.async_api import async_playwright

        observed_json: list[tuple[str, object]] = []
        observed_requests: dict[str, dict] = {}
        warnings: list[str] = []
        pages: list[BrowserJsonPage] = []
        browser = None
        context = None

        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                context = await browser.new_context(
                    accept_downloads=False, service_workers="block", java_script_enabled=True,
                    user_agent=plan.user_agent,
                )
                context.set_default_timeout(plan.max_total_ms)
                page = await context.new_page()
                context.on("page", lambda p: _safe_close(p))
                await self._install_guards(
                    context, plan, observed_json, observed_requests, warnings
                )
                response = await page.goto(source_page_url, wait_until="domcontentloaded")
                status = response.status if response is not None else 0
                html = await page.content()
                challenge = _challenge_reason(html, status)
                if challenge is not None:
                    return BrowserPagedResult(
                        final_url=page.url, status_code=status,
                        blocked_reason=challenge, warnings=tuple(warnings),
                    )
                await self._run_plan(page, plan, warnings)

                for _ in range(max_pages):
                    url = next_url(pages, observed_json)
                    if not url:
                        break
                    if not await _host_allowed(url):
                        warnings.append(f"ssrf_blocked:{urlsplit(url).hostname}")
                        break
                    fetched = await page.evaluate(
                        """async (u) => {
                            const r = await fetch(u, {credentials: 'include'});
                            const t = await r.text();
                            let j = null;
                            try { j = JSON.parse(t); } catch (e) { j = null; }
                            return { status: r.status, json: j };
                        }""",
                        url,
                    )
                    pages.append(
                        BrowserJsonPage(
                            url=url, status=int(fetched.get("status", 0)),
                            json=fetched.get("json"),
                        )
                    )
                return BrowserPagedResult(
                    final_url=page.url, status_code=status, pages=pages,
                    warnings=tuple(warnings),
                )
        except Exception as exc:  # noqa: BLE001 - a fetch/launch failure must not crash the app
            return BrowserPagedResult(
                final_url=source_page_url, status_code=0, pages=pages,
                blocked_reason=f"browser_error:{type(exc).__name__}",
                warnings=tuple(warnings),
            )
        finally:
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


class BrowserRenderFetchStrategy:
    """A `FetchStrategy` that renders the URL in the restricted headless browser
    and returns the rendered HTML as an ordinary `FetchResponse`, so the same
    HTML extraction patterns (e.g. generic_html_cards) run against a JS-rendered
    page. This is the transport for sources whose events only exist after
    client-side rendering, or whose data API is edge-protected — the browser
    passes the same edge/bot checks a real visitor would.

    `plan` is a closed BrowserPlan (waits/scrolls/clicks); `_browser` is a
    testability seam so tests can inject a fake render without launching a real
    browser. Query params / JSON bodies on the request are ignored — a browser
    navigation is a GET of the URL, exactly like a real visitor."""

    def __init__(
        self, *, plan: BrowserPlan | None = None, browser: BrowserFetchStrategy | None = None
    ) -> None:
        self._plan = plan
        self._browser = browser or BrowserFetchStrategy()

    async def fetch(self, request: FetchRequest, config: FetchConfig) -> FetchResponse:
        result = await self._browser.render(request.url, self._plan)
        body = (result.rendered_html or "").encode("utf-8")
        blocked = result.blocked_reason
        # A render that returned no HTML without an explicit reason is still a
        # failure the pipeline must see, not a silent empty page.
        if blocked is None and not body:
            blocked = "browser_render_empty"
        return FetchResponse(
            request_url=request.url,
            final_url=result.final_url,
            status_code=result.status_code,
            headers={"content-type": "text/html; charset=utf-8"},
            content_type="text/html",
            body=body,
            redirect_history=(),
            body_hash=hashlib.sha256(body).hexdigest(),
            elapsed_seconds=0.0,
            blocked_reason=blocked,
        )


def _get_at_path(obj, dotted: str):
    node = obj
    for seg in dotted.split("."):
        if isinstance(node, dict) and seg in node:
            node = node[seg]
        else:
            return None
    return node


def _set_at_path(obj: dict, dotted: str, value) -> dict:
    node = obj
    segs = dotted.split(".")
    for seg in segs[:-1]:
        nxt = node.get(seg)
        if not isinstance(nxt, dict):
            nxt = {}
            node[seg] = nxt
        node = nxt
    node[segs[-1]] = value
    return obj


def _records_at(payload, record_path: str) -> list:
    found = _get_at_path(payload, record_path)
    return [r for r in found if isinstance(r, dict)] if isinstance(found, list) else []


# Candidate paths for a "total" count, tried in order after any configured path.
_TOTAL_PATH_CANDIDATES = ("docs.total", "docs.count", "total", "count", "meta.total")
# Stable-identifier fields for cross-page dedup, best first.
_RECORD_ID_FIELDS = ("recid", "_id", "id", "uuid", "url")
# A per-occurrence date component, so recurring instances that share a base id
# but occur on different dates are NOT collapsed together.
_RECORD_DATE_FIELDS = ("startDate", "start_date", "startdate", "date", "start")
# Fields whose integer value is an offset cursor / a page size, when inferring
# pagination from a request the page itself made (no stored recipe). Generic
# REST conventions — never a provider name.
_OFFSET_CURSOR_FIELDS = frozenset({"skip", "offset", "start", "from"})
_LIMIT_CURSOR_FIELDS = frozenset({"limit", "pagesize", "page_size", "size", "per_page", "perpage"})


def _detect_total(payload, configured_path: str | None) -> int | None:
    for path in ((configured_path,) if configured_path else ()) + _TOTAL_PATH_CANDIDATES:
        value = _get_at_path(payload, path)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _record_identity(record: dict) -> str:
    base = None
    for field_name in _RECORD_ID_FIELDS:
        value = record.get(field_name)
        if value not in (None, "", [], {}):
            base = f"{field_name}:{value}"
            break
    if base is None:
        import json as _json

        base = "hash:" + hashlib.sha256(
            _json.dumps(record, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
    for date_field in _RECORD_DATE_FIELDS:
        value = record.get(date_field)
        if isinstance(value, (str, int)) and value not in (None, ""):
            return f"{base}|{value}"
    return base


def _find_int_field_path(node, names: frozenset, prefix: tuple = ()) -> list | None:
    """Path (list of keys) to the first integer-valued (non-bool) key whose name
    is in `names`, searching a decoded JSON object depth-first. Used to locate a
    pagination cursor inside a request the page itself made."""
    if isinstance(node, dict):
        for key, value in node.items():
            if (
                str(key).lower() in names
                and isinstance(value, int)
                and not isinstance(value, bool)
            ):
                return [*prefix, str(key)]
        for key, value in node.items():
            found = _find_int_field_path(value, names, (*prefix, str(key)))
            if found is not None:
                return found
    return None


def _infer_cursor(url: str) -> tuple[str | None, list | None, int | None]:
    """Infer (json-param name, offset path, page size) from a request URL the
    page actually made, by finding an integer offset field (skip/offset/…) and a
    sibling page-size field inside a JSON query parameter. Enables pagination for
    configurations that have no stored recipe. (None, None, None) if not found."""
    import json as _json

    parts = urlsplit(url)
    for name, raw in parse_qsl(parts.query, keep_blank_values=True):
        try:
            decoded = _json.loads(raw)
        except (ValueError, TypeError):
            continue
        if not isinstance(decoded, dict):
            continue
        offset_path = _find_int_field_path(decoded, _OFFSET_CURSOR_FIELDS)
        if offset_path is None:
            continue
        limit_path = _find_int_field_path(decoded, _LIMIT_CURSOR_FIELDS)
        limit = _get_at_path(decoded, ".".join(limit_path)) if limit_path else None
        return name, offset_path, (limit if isinstance(limit, int) else None)
    return None, None, None


def _find_placeholder_path(node, kind: str, prefix: tuple = ()) -> list | None:
    """Path (list of dict keys) to a bare `{"kind": <kind>}` placeholder inside a
    recipe json template, or None. Used to locate WHERE the offset cursor lives
    in the request body — per configuration, never a hardcoded field name."""
    if isinstance(node, dict):
        if set(node.keys()) == {"kind"} and node.get("kind") == kind:
            return list(prefix)
        for key, value in node.items():
            found = _find_placeholder_path(value, kind, (*prefix, str(key)))
            if found is not None:
                return found
    return None


def _locate_offset(recipe) -> tuple[str | None, list | None]:
    """(query-param name, path) of the offset cursor within a recipe's json
    template, derived from its `page_offset` placeholder. (None, None) if absent."""
    for name, value in (recipe.query_params or {}).items():
        if getattr(value, "kind", None) == "json_template":
            path = _find_placeholder_path(value.value, "page_offset")
            if path is not None:
                return name, path
    return None, None


def _url_with_offset(url: str, json_param: str, offset_path: list, offset: int) -> str | None:
    """Return `url` with only the offset cursor changed: decode the json query
    param, set the value at `offset_path`, re-encode. Every other byte of the
    request (token, filters, the rendered date window) is preserved exactly —
    this is why it passes the edge/bot check that a reconstructed request fails."""
    import json as _json

    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    raw = query.get(json_param)
    if raw is None:
        return None
    try:
        decoded = _json.loads(raw)
    except (ValueError, TypeError):
        return None
    _set_at_path(decoded, ".".join(offset_path), offset)
    query[json_param] = _json.dumps(decoded)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


class BrowserStructuredResponseFetchStrategy:
    """A `FetchStrategy` that navigates the *source page* in the restricted
    browser and returns the page's own JSON event data as one application/json
    `FetchResponse` — so the ordinary structured extractor (e.g. simpleview_events
    reading docs.docs) runs against the FULL result set.

    This is the browser mode for an event-like JSON endpoint a normalized HTTP
    client cannot reach (the site's edge/bot protection only lets the request
    through from inside a real browser session). When the configuration carries a
    request recipe with offset pagination, the browser navigates once and then
    walks every page from within the page context via its own `fetch()` — same
    origin, cookies, Referer and (crucially) the same rendered date window; only
    the offset cursor changes. Records are combined and deduplicated across pages
    before extraction. Without a paginating recipe it falls back to capturing the
    single response the page already made.

    `_browser` is a testability seam; nothing here is provider-specific — the
    record path, total path and cursor position all come from the configuration
    (record_path) and the captured recipe (offset placeholder, limit, total)."""

    def __init__(
        self,
        *,
        source_page_url: str,
        endpoint_match: str,
        plan: BrowserPlan | None = None,
        browser: BrowserFetchStrategy | None = None,
        recipe=None,
        record_path: str = "docs.docs",
        timezone: str | None = None,
        max_pages: int = 20,
        max_records: int = 2000,
        now_utc=None,
    ) -> None:
        self._source_page_url = source_page_url
        self._endpoint_match = endpoint_match
        self._plan = plan
        self._browser = browser or BrowserFetchStrategy()
        self._recipe = recipe
        self._record_path = record_path or "docs.docs"
        self._timezone = timezone
        self._max_pages = max(1, max_pages)
        self._max_records = max(1, max_records)
        self._now_utc = now_utc

    def _match_signature(self) -> str:
        path = urlsplit(self._endpoint_match).path
        return path or self._endpoint_match

    def _select(self, observed: list[tuple[str, object]]) -> tuple[str, object] | None:
        from app.extraction.structured_candidates import is_telemetry

        signature = self._match_signature()
        for url, payload in observed:
            if signature and signature not in url:
                continue
            if is_telemetry(url):
                continue
            if isinstance(payload, (dict, list)):
                return url, payload
        return None

    def _json_response(self, payload, *, final_url, pagination=None) -> FetchResponse:
        import json as _json

        body = _json.dumps(payload).encode("utf-8")
        return FetchResponse(
            request_url=self._source_page_url, final_url=final_url, status_code=200,
            headers={"content-type": "application/json"}, content_type="application/json",
            body=body, redirect_history=(), body_hash=hashlib.sha256(body).hexdigest(),
            elapsed_seconds=0.0, blocked_reason=None, pagination=pagination,
        )

    def _blocked_response(
        self, *, final_url, status_code, reason, pagination=None
    ) -> FetchResponse:
        return FetchResponse(
            request_url=self._source_page_url, final_url=final_url, status_code=status_code,
            headers={}, content_type=None, body=b"", redirect_history=(),
            body_hash=hashlib.sha256(b"").hexdigest(), elapsed_seconds=0.0,
            blocked_reason=reason, pagination=pagination,
        )

    async def fetch(self, request: FetchRequest, config: FetchConfig) -> FetchResponse:
        # A structured browser config always walks every page. Pagination degrades
        # gracefully to a single page when no offset cursor can be determined.
        return await self._fetch_paginated()

    def _resolve_cursor(self, seed_url: str) -> tuple[str | None, list | None, int | None]:
        """(json-param, offset path, page size) for pagination. Prefer the stored
        recipe's page_offset placeholder + limit; otherwise INFER them from the
        page's own request URL — so a configuration with no recipe (e.g. an older
        approved one) still paginates without being mutated or re-versioned."""
        recipe = self._recipe
        if recipe is not None and recipe.pagination.kind == "offset":
            json_param, offset_path = _locate_offset(recipe)
            if json_param and offset_path:
                return json_param, offset_path, recipe.pagination.limit
        return _infer_cursor(seed_url)

    async def _fetch_paginated(self) -> FetchResponse:
        record_path = self._record_path
        total_path = self._recipe.pagination.total_path if self._recipe is not None else None
        state = {
            "processed": 0, "records": [], "seen": set(), "total": None, "raw": 0,
            "rows": [], "stop": None, "last_status": 200, "last_returned": 0, "last_new": 0,
            "last_json_ok": True, "seed_url": None, "json_param": None, "offset_path": None,
            "limit": None, "first_count": None, "incomplete": False, "failed_offset": None,
        }

        def _effective_limit() -> int:
            return state["limit"] or state["first_count"] or 0

        def _process(pages: list) -> None:
            while state["processed"] < len(pages):
                page = pages[state["processed"]]
                index = state["processed"]
                state["processed"] += 1
                if state["total"] is None and page.json is not None:
                    state["total"] = _detect_total(page.json, total_path)
                records = _records_at(page.json, record_path) if page.json is not None else []
                if index == 0:
                    state["first_count"] = len(records)
                state["raw"] += len(records)
                new = 0
                for record in records:
                    identity = _record_identity(record)
                    if identity in state["seen"]:
                        continue
                    state["seen"].add(identity)
                    state["records"].append(record)
                    new += 1
                state["last_status"] = page.status
                state["last_returned"] = len(records)
                state["last_new"] = new
                state["last_json_ok"] = page.json is not None
                state["rows"].append(
                    {
                        "index": index, "returned": len(records), "new": new,
                        "cumulative_unique": len(state["records"]), "status": page.status,
                    }
                )

        def _fail(reason: str, offset: int) -> None:
            state["stop"] = reason
            if state["records"]:  # page 1 already gave us data — the walk is partial
                state["incomplete"] = True
                state["failed_offset"] = offset

        def _next_url(pages: list, observed: list) -> str | None:
            _process(pages)
            index = len(pages)
            if index == 0:
                # Page 1 is the page's OWN request (byte-identical to what its JS
                # sent) — the only form the edge/bot check reliably lets through.
                selected = self._select(observed)
                if selected is None:
                    state["stop"] = "no_observed_response"
                    return None
                state["seed_url"] = selected[0]
                jp, op, lim = self._resolve_cursor(state["seed_url"])
                state["json_param"], state["offset_path"], state["limit"] = jp, op, lim
                return state["seed_url"]
            limit = _effective_limit()
            failed_offset = (index - 1) * limit
            if not (200 <= state["last_status"] < 300):
                _fail("non_2xx_status", failed_offset)
                return None
            if not state["last_json_ok"]:
                _fail("invalid_json", failed_offset)
                return None
            if state["last_returned"] == 0:
                state["stop"] = "empty_page"
                return None
            if state["last_new"] == 0:
                state["stop"] = "no_new_records"
                return None
            if limit and state["last_returned"] < limit:
                state["stop"] = "short_page"
                return None
            if state["total"] is not None and len(state["records"]) >= state["total"]:
                state["stop"] = "reported_total_reached"
                return None
            if len(state["records"]) >= self._max_records:
                state["stop"] = "max_records"
                return None
            if index >= self._max_pages:
                state["stop"] = "max_pages"
                return None
            if not (state["seed_url"] and state["json_param"] and state["offset_path"] and limit):
                state["stop"] = "no_offset_cursor"
                return None
            # Subsequent pages: the same request with ONLY the offset advanced.
            return _url_with_offset(
                state["seed_url"], state["json_param"], state["offset_path"], index * limit
            )

        result = await self._browser.render_and_fetch_json_pages(
            self._source_page_url, self._plan, next_url=_next_url, max_pages=self._max_pages,
        )
        if result.blocked_reason is not None and not state["records"]:
            return self._blocked_response(
                final_url=result.final_url, status_code=result.status_code,
                reason=result.blocked_reason,
            )
        _process(result.pages)  # process any trailing page
        if state["stop"] is None:
            state["stop"] = "max_pages" if len(result.pages) >= self._max_pages else "exhausted"

        limit = _effective_limit()
        pagination = {
            "kind": "offset", "page_size": limit, "pages_fetched": len(state["rows"]),
            "reported_total": state["total"], "raw_records": state["raw"],
            "captured_records": len(state["records"]), "unique_records": len(state["records"]),
            "duplicates_discarded": state["raw"] - len(state["records"]),
            "stop_reason": state["stop"], "incomplete": state["incomplete"],
            "offsets": [row["index"] * limit for row in state["rows"]],
            "pages": [
                {
                    "page": row["index"] + 1, "offset": row["index"] * limit,
                    "returned": row["returned"], "new": row["new"],
                    "cumulative_unique": row["cumulative_unique"], "status": row["status"],
                }
                for row in state["rows"]
            ],
        }
        if state["incomplete"]:
            pagination["failed_offset"] = state["failed_offset"]

        if not state["records"]:
            return self._blocked_response(
                final_url=result.final_url, status_code=result.status_code,
                reason="browser_no_structured_response", pagination=pagination,
            )
        if state["incomplete"]:
            # Page 1 succeeded but a later page failed: do NOT report a complete
            # success with a partial result set. Block so nothing is imported
            # (all-or-nothing) while keeping the diagnostics that show which
            # offset failed.
            return self._blocked_response(
                final_url=result.pages[0].url if result.pages else result.final_url,
                status_code=result.status_code,
                reason=f"browser_pagination_incomplete:offset_{state['failed_offset']}",
                pagination=pagination,
            )

        import copy

        first_json = result.pages[0].json if result.pages and result.pages[0].json else {}
        combined = copy.deepcopy(first_json) if isinstance(first_json, dict) else {}
        _set_at_path(combined, record_path, state["records"])
        final_url = result.pages[0].url if result.pages else result.final_url
        return self._json_response(combined, final_url=final_url, pagination=pagination)


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
