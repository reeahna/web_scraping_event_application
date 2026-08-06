"""Render, capture, and summarize request recipes.

Rendering resolves the dynamic placeholders (date window, pagination cursor,
source-page URL) into a concrete request the ordinary HTTP fetch strategy can
send — the *same* path used for both preview and import. Capture turns a
browser-observed successful request into a normalized, reusable recipe: nested
JSON parameters are decoded, capture-time dates matching the observation window
become dynamic placeholders, pagination fields are recognized, public tokens
and a required Referer are preserved, and unsafe headers (cookies, auth,
fingerprint, cache, transport) are discarded.

Nothing here is provider-specific: parameter/header names and JSON shapes come
from the observed request, never hardcoded.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from app.schemas.http_safety import FORBIDDEN_HEADERS
from app.schemas.request_recipe import (
    TEMPLATE_PLACEHOLDER_KINDS,
    RecipePagination,
    RecipeValue,
    RecipeWindow,
    RequestRecipe,
)

# Request headers safe to persist and replay. Everything else (cookies, auth,
# sec-ch-* fingerprint, cache, and transport headers like Host/Connection/
# Content-Length/Accept-Encoding) is discarded at capture time.
_SAFE_HEADER_ALLOWLIST = frozenset(
    {"accept", "referer", "accept-language", "content-type", "x-requested-with"}
)

# Query-parameter names that carry an opaque site token — redacted in display
# and logs, preserved in the executable configuration.
_TOKEN_PARAM_NAMES = frozenset({"token", "key", "apikey", "api_key", "app_id", "appid", "sig"})

# Keys inside a JSON template whose numeric value is a pagination cursor.
_LIMIT_KEYS = frozenset({"limit", "pagesize", "page_size", "count", "size", "per_page", "perpage"})
_OFFSET_KEYS = frozenset({"skip", "offset", "start", "from"})

# Keys whose (date-valued) content is the request's forward-window boundary.
# Generic REST/Mongo range conventions — not provider-specific. A key only
# triggers boundary substitution when its value is actually a date (bare ISO or
# a `{"$date": "..."}` wrapper); an integer under the same key (e.g. "start": 0)
# is handled by the pagination-offset branch instead.
_WINDOW_START_KEYS = frozenset(
    {"start", "from", "after", "begin", "start_date", "startdate", "date_from", "datefrom", "gte"}
)
_WINDOW_END_KEYS = frozenset(
    {"end", "to", "before", "finish", "end_date", "enddate", "date_to", "dateto", "lte"}
)

# How close a captured date must be to the observation window boundary to be
# treated as that dynamic boundary (conservative — unrelated dates are kept).
# Fallback for bare dates that are NOT under a recognised range-boundary key.
_WINDOW_MATCH_TOLERANCE = timedelta(days=2)


# --- rendering ---------------------------------------------------------------


@dataclass
class RenderedRequest:
    method: str
    endpoint: str
    params: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    json_body: dict | None = None


@dataclass
class _RenderContext:
    window_start_iso: str
    window_end_iso: str
    page_limit: int
    page_offset: int
    page_number: int
    source_page_url: str | None


def _iso_ms_utc(dt: datetime) -> str:
    """UTC ISO-8601 with millisecond precision and a Z suffix, e.g.
    2026-08-06T04:00:00.000Z."""
    dt = dt.astimezone(UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{dt.microsecond // 1000:03d}Z"


def _window_bounds(
    window: RecipeWindow, now_utc: datetime, timezone: str | None
) -> tuple[str, str]:
    tz = ZoneInfo(timezone) if timezone else UTC
    local = now_utc.astimezone(tz)
    if window.start_of_day:
        local = local.replace(hour=0, minute=0, second=0, microsecond=0)
    start = local
    # Inclusive end: the last millisecond of the horizon's final day.
    end = start + timedelta(days=window.horizon_days) - timedelta(milliseconds=1)
    return _iso_ms_utc(start), _iso_ms_utc(end)


def _placeholder_value(kind: str, ctx: _RenderContext):
    if kind == "window_start_utc":
        return ctx.window_start_iso
    if kind == "window_end_utc":
        return ctx.window_end_iso
    if kind == "page_limit":
        return ctx.page_limit
    if kind == "page_offset":
        return ctx.page_offset
    if kind == "page_number":
        return ctx.page_number
    if kind == "source_page_url":
        return ctx.source_page_url
    return None


def _is_placeholder(node) -> bool:
    return (
        isinstance(node, dict)
        and list(node.keys()) == ["kind"]
        and node["kind"] in TEMPLATE_PLACEHOLDER_KINDS
    )


def _substitute(node, ctx: _RenderContext):
    if _is_placeholder(node):
        return _placeholder_value(node["kind"], ctx)
    if isinstance(node, dict):
        return {k: _substitute(v, ctx) for k, v in node.items()}
    if isinstance(node, list):
        return [_substitute(item, ctx) for item in node]
    return node


def _render_value(rv: RecipeValue, ctx: _RenderContext, *, as_json_string: bool):
    if rv.kind == "source_page_url":
        return ctx.source_page_url or ""
    if rv.kind == "json_template":
        rendered = _substitute(rv.value, ctx)
        return json.dumps(rendered, separators=(",", ":")) if as_json_string else rendered
    # literal
    value = rv.value
    if as_json_string and not isinstance(value, str):
        return (
            json.dumps(value, separators=(",", ":"))
            if isinstance(value, (list, dict))
            else str(value)
        )
    return value


def render_recipe(
    recipe: RequestRecipe,
    *,
    now_utc: datetime,
    timezone: str | None = None,
    page_offset: int = 0,
    page_number: int = 1,
) -> RenderedRequest:
    """Resolve a recipe into a concrete request. `now_utc` is passed in (not
    read from the clock) so rendering is deterministic and testable."""
    start_iso, end_iso = _window_bounds(recipe.window, now_utc, timezone)
    ctx = _RenderContext(
        window_start_iso=start_iso,
        window_end_iso=end_iso,
        page_limit=recipe.pagination.limit,
        page_offset=page_offset,
        page_number=page_number,
        source_page_url=recipe.source_page_url,
    )
    params: dict[str, str] = {}
    for name, rv in recipe.query_params.items():
        rendered = _render_value(rv, ctx, as_json_string=True)
        params[name] = rendered if isinstance(rendered, str) else str(rendered)

    headers: dict[str, str] = {}
    for name, rv in recipe.headers.items():
        rendered = _render_value(rv, ctx, as_json_string=False)
        headers[name] = rendered if isinstance(rendered, str) else str(rendered)

    json_body = None
    if recipe.body is not None:
        rendered_body = _render_value(recipe.body, ctx, as_json_string=False)
        json_body = rendered_body if isinstance(rendered_body, dict) else None

    return RenderedRequest(
        method=recipe.method,
        endpoint=recipe.endpoint,
        params=params,
        headers=headers,
        json_body=json_body,
    )


# --- capture / normalization -------------------------------------------------


def _parse_iso(value: str) -> datetime | None:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _boundary_kind_for_key(lowered_key: str) -> str | None:
    if lowered_key in _WINDOW_START_KEYS:
        return "window_start_utc"
    if lowered_key in _WINDOW_END_KEYS:
        return "window_end_utc"
    return None


def _as_date_boundary(value, kind: str):
    """If `value` is an ISO datetime — either bare or wrapped in a MongoDB
    extended-JSON `{"$date": "..."}` object — return the placeholder form,
    preserving the wrapper. Otherwise return None (leave the value untouched)."""
    if (
        isinstance(value, dict)
        and set(value.keys()) == {"$date"}
        and isinstance(value["$date"], str)
        and _parse_iso(value["$date"]) is not None
    ):
        return {"$date": {"kind": kind}}
    if isinstance(value, str) and _parse_iso(value) is not None:
        return {"kind": kind}
    return None


def _normalize_template(node, *, window_start: datetime | None, window_end: datetime | None):
    """Replace request-window dates and pagination cursors with placeholders.

    Conservative on two axes:
    - Dates only become window placeholders when they sit under a recognised
      range-boundary key (start/from/after… → start, end/to/before… → end) or,
      as a fallback, when a bare date matches an observation-window boundary
      within tolerance. Unrelated dates (createdDate, publishedAt, …) stay
      literal.
    - Pagination cursors only when the key is a known limit/offset key and the
      value is a real integer (not a bool).

    None of the recognised keys are provider-specific — start/end, from/to,
    limit/skip are generic REST/Mongo conventions."""
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            lowered = str(key).lower()
            if (
                lowered in _LIMIT_KEYS
                and isinstance(value, (int, float))
                and not isinstance(value, bool)
            ):
                out[key] = {"kind": "page_limit"}
            elif (
                lowered in _OFFSET_KEYS
                and isinstance(value, (int, float))
                and not isinstance(value, bool)
            ):
                out[key] = {"kind": "page_offset"}
            elif (
                boundary_kind := _boundary_kind_for_key(lowered)
            ) is not None and _as_date_boundary(value, boundary_kind) is not None:
                out[key] = _as_date_boundary(value, boundary_kind)
            else:
                out[key] = _normalize_template(
                    value, window_start=window_start, window_end=window_end
                )
        return out
    if isinstance(node, list):
        return [
            _normalize_template(v, window_start=window_start, window_end=window_end) for v in node
        ]
    if isinstance(node, str):
        parsed = _parse_iso(node)
        if parsed is not None:
            if window_start and abs(parsed - window_start) <= _WINDOW_MATCH_TOLERANCE:
                return {"kind": "window_start_utc"}
            if window_end and abs(parsed - window_end) <= _WINDOW_MATCH_TOLERANCE:
                return {"kind": "window_end_utc"}
    return node


def _looks_like_json(value: str) -> bool:
    stripped = value.strip()
    return stripped.startswith("{") or stripped.startswith("[")


def _sanitize_headers(
    headers: dict[str, str], *, source_page_url: str | None
) -> dict[str, RecipeValue]:
    safe: dict[str, RecipeValue] = {}
    for name, value in (headers or {}).items():
        lowered = name.lower()
        if lowered in FORBIDDEN_HEADERS or lowered not in _SAFE_HEADER_ALLOWLIST:
            continue
        if (
            lowered == "referer"
            and source_page_url
            and value.rstrip("/") == source_page_url.rstrip("/")
        ):
            safe[name] = RecipeValue(kind="source_page_url")
        else:
            safe[name] = RecipeValue(kind="literal", value=value)
    return safe


def capture_recipe(
    *,
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    request_body: str | None = None,
    source_page_url: str | None = None,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    horizon_days: int | None = None,
) -> RequestRecipe:
    """Build a normalized, reusable recipe from an observed successful request.
    Site-agnostic — every parameter/header/shape comes from the observation."""
    parts = urlsplit(url)
    endpoint = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))

    captured_limit: int | None = None
    query_params: dict[str, RecipeValue] = {}
    for name, value in parse_qsl(parts.query, keep_blank_values=True):
        if _looks_like_json(value):
            try:
                decoded = json.loads(value)
            except (json.JSONDecodeError, ValueError):
                query_params[name] = RecipeValue(kind="literal", value=value)
                continue
            captured_limit = captured_limit or _find_limit_in_template(decoded)
            template = _normalize_template(
                decoded, window_start=window_start, window_end=window_end
            )
            query_params[name] = RecipeValue(kind="json_template", value=template)
        else:
            query_params[name] = RecipeValue(kind="literal", value=value)

    body_value: RecipeValue | None = None
    if method.upper() == "POST" and request_body and _looks_like_json(request_body):
        try:
            decoded_body = json.loads(request_body)
            captured_limit = captured_limit or _find_limit_in_template(decoded_body)
            body_value = RecipeValue(
                kind="json_template",
                value=_normalize_template(
                    decoded_body, window_start=window_start, window_end=window_end
                ),
            )
        except (json.JSONDecodeError, ValueError):
            body_value = None

    # Pagination: offset-based when a skip/offset cursor was recognized.
    has_offset = _has_placeholder(query_params, "page_offset") or (
        body_value is not None and _template_has_placeholder(body_value.value, "page_offset")
    )
    pagination = RecipePagination(
        kind="offset" if has_offset else "none",
        limit=min(max(captured_limit or 100, 1), 1000),
        # Best-effort default total-count location (Mongo `find` convention); a
        # wrong path simply never fires and the other stop conditions apply.
        total_path="docs.count" if has_offset else None,
    )

    window = RecipeWindow(horizon_days=horizon_days or 30, start_of_day=True)

    return RequestRecipe(
        method=method.upper(),
        endpoint=endpoint,
        query_params=query_params,
        headers=_sanitize_headers(headers or {}, source_page_url=source_page_url),
        body=body_value,
        source_page_url=source_page_url,
        window=window,
        pagination=pagination,
    )


def _template_has_placeholder(node, kind: str) -> bool:
    if _is_placeholder(node) and node.get("kind") == kind:
        return True
    if isinstance(node, dict):
        return any(_template_has_placeholder(v, kind) for v in node.values())
    if isinstance(node, list):
        return any(_template_has_placeholder(v, kind) for v in node)
    return False


def _has_placeholder(query_params: dict[str, RecipeValue], kind: str) -> bool:
    return any(
        rv.kind == "json_template" and _template_has_placeholder(rv.value, kind)
        for rv in query_params.values()
    )


def _find_limit_in_template(node) -> int | None:
    if isinstance(node, dict):
        for key, value in node.items():
            if (
                str(key).lower() in _LIMIT_KEYS
                and isinstance(value, int)
                and not isinstance(value, bool)
            ):
                return value
            found = _find_limit_in_template(value)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_limit_in_template(item)
            if found is not None:
                return found
    return None


# --- safe display ------------------------------------------------------------


def redact_token(value: str) -> str:
    """A short, non-reversible hint of a token value for UI/logs."""
    if not isinstance(value, str) or not value:
        return "—"
    if len(value) <= 6:
        return "•••"
    return f"{value[:3]}…{value[-2:]} ({len(value)} chars)"


def _template_has_dynamic_dates(
    query_params: dict[str, RecipeValue], body: RecipeValue | None
) -> bool:
    nodes = [rv.value for rv in query_params.values() if rv.kind == "json_template"]
    if body is not None and body.kind == "json_template":
        nodes.append(body.value)
    return any(
        _template_has_placeholder(node, "window_start_utc")
        or _template_has_placeholder(node, "window_end_utc")
        for node in nodes
    )


def summarize_recipe(recipe: RequestRecipe | None) -> dict | None:
    """A safe, token-redacted summary for admin diagnostics — parameter *names*
    only, never raw values or the full query string."""
    if recipe is None:
        return None
    token_params = [n for n in recipe.query_params if n.lower() in _TOKEN_PARAM_NAMES]
    has_referer = any(n.lower() == "referer" for n in recipe.headers)
    return {
        "method": recipe.method,
        "endpoint": recipe.endpoint,
        "query_param_names": sorted(recipe.query_params.keys()),
        "body_type": (recipe.body.kind if recipe.body else "none"),
        "pagination_kind": recipe.pagination.kind,
        "dynamic_date_window": _template_has_dynamic_dates(recipe.query_params, recipe.body),
        "referer_present": has_referer,
        "public_token_present": bool(token_params),
        "public_token_hint": (
            redact_token(str(recipe.query_params[token_params[0]].value))
            if token_params and recipe.query_params[token_params[0]].kind == "literal"
            else None
        ),
        "horizon_days": recipe.window.horizon_days,
    }
