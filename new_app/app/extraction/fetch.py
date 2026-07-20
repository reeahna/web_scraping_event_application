"""Async HTTP fetch strategy with connect-time SSRF protection.

No Playwright, no browser network interception. httpx only. Every request —
including every redirect hop — goes through `validate_request_url()` +
`resolve_and_validate_host()` before a socket is opened. Redirects are always
followed manually (`follow_redirects=False`), never by httpx itself, so
there is no code path that can skip per-hop validation.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Protocol
from urllib.parse import urljoin

import httpx

from app.extraction.network_safety import (
    BlockedHostError,
    hostname_and_port,
    resolve_and_validate_host,
    validate_request_url,
)
from app.extraction.types import FetchRequest, FetchResponse
from app.schemas.extraction import FetchConfig

# Deterministic, fixed — never overridable via site configuration (headers
# merge *under* this value; see _merge_headers).
USER_AGENT = "CityEventsBot/1.0 (+https://example.invalid/bot-info)"

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_RETRYABLE_STATUSES = frozenset({500, 502, 503, 504})

# Short, frozen — deliberately not a growing heuristic pile.
_BLOCK_MARKERS: tuple[str, ...] = ("cloudflare", "access denied", "are you a robot", "captcha")


class FetchStrategy(Protocol):
    async def fetch(self, request: FetchRequest, config: FetchConfig) -> FetchResponse: ...


def _merge_headers(configured: dict[str, str]) -> dict[str, str]:
    merged = {k: v for k, v in configured.items() if k.lower() != "user-agent"}
    merged["User-Agent"] = USER_AGENT
    return merged


def _blocked_marker(status_code: int, body_sample: bytes) -> str | None:
    if status_code in (403, 429):
        return f"http_{status_code}"
    lowered = body_sample.lower()
    for marker in _BLOCK_MARKERS:
        if marker.encode("utf-8") in lowered:
            return f"challenge_marker:{marker}"
    return None


def content_type_allowed(content_type: str | None, config: FetchConfig) -> bool:
    """Used by callers (app.services.extraction_runs) to decide whether a
    successful-status response should still be treated as `failed` because
    its Content-Type isn't one this site's configuration expects."""
    if content_type is None:
        return True
    base_type = content_type.split(";", 1)[0].strip().lower()
    return any(base_type == allowed.lower() for allowed in config.allowed_content_types)


def _blocked_response(
    request_url: str, final_url: str, reason: str, elapsed: float
) -> FetchResponse:
    return FetchResponse(
        request_url=request_url,
        final_url=final_url,
        status_code=0,
        headers={},
        content_type=None,
        body=b"",
        redirect_history=(),
        body_hash=hashlib.sha256(b"").hexdigest(),
        elapsed_seconds=elapsed,
        blocked_reason=reason,
    )


class HttpFetchStrategy:
    """The one real FetchStrategy implementation.

    `transport` is a testability hook only — production code always uses the
    default (`None`), which makes httpx open real connections. Tests pass an
    `httpx.MockTransport` so fetch/redirect/retry/blocked-response behavior
    can be exercised deterministically with zero live network calls, while
    connect-time SSRF validation (which runs *before* the transport is ever
    invoked) stays fully exercised.
    """

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    async def fetch(self, request: FetchRequest, config: FetchConfig) -> FetchResponse:
        start = time.monotonic()
        redirect_history: list[str] = []
        current_url = request.url
        headers = _merge_headers(request.headers)
        timeout = httpx.Timeout(
            connect=config.connect_timeout_seconds,
            read=config.read_timeout_seconds,
            write=config.read_timeout_seconds,
            pool=config.connect_timeout_seconds,
        )

        for hop in range(config.max_redirects + 1):
            try:
                safe_url = validate_request_url(current_url)
                hostname, port = hostname_and_port(safe_url)
                await resolve_and_validate_host(hostname, port)
            except BlockedHostError as exc:
                return _blocked_response(
                    request.url, current_url, f"ssrf_blocked:{exc}", time.monotonic() - start
                )

            outcome = await self._attempt_with_retries(
                safe_url,
                method=request.method,
                headers=headers,
                params=request.params if hop == 0 else {},
                json_body=request.json_body if hop == 0 else None,
                config=config,
                timeout=timeout,
            )

            if isinstance(outcome, str):
                # A transient/terminal fetch failure, already reason-coded.
                return _blocked_response(request.url, safe_url, outcome, time.monotonic() - start)

            status_code, response_headers, body, truncated = outcome

            if status_code in _REDIRECT_STATUSES and "location" in response_headers:
                redirect_history.append(safe_url)
                current_url = urljoin(safe_url, response_headers["location"])
                continue

            blocked_reason = _blocked_marker(status_code, body[:2000])
            content_type = response_headers.get("content-type")
            # Content-type mismatch is surfaced via `content_type` for the
            # caller to reject as `failed` — it is not itself a `blocked`
            # signal, so it never overrides an already-detected block marker.
            return FetchResponse(
                request_url=request.url,
                final_url=safe_url,
                status_code=status_code,
                headers=response_headers,
                content_type=content_type,
                body=body,
                redirect_history=tuple(redirect_history),
                body_hash=hashlib.sha256(body).hexdigest(),
                elapsed_seconds=time.monotonic() - start,
                blocked_reason=blocked_reason,
                truncated=truncated,
            )

        return _blocked_response(
            request.url, current_url, "too_many_redirects", time.monotonic() - start
        )

    async def _attempt_with_retries(
        self,
        url: str,
        *,
        method: str,
        headers: dict[str, str],
        params: dict[str, str],
        json_body: dict | None,
        config: FetchConfig,
        timeout: httpx.Timeout,
    ) -> tuple[int, dict[str, str], bytes, bool] | str:
        """Returns (status_code, headers, body, truncated) on any response
        received, or a short reason-code string on a terminal fetch failure
        (never raises — fetch failures must not crash the application)."""
        last_reason = "failed:unknown"
        for attempt in range(config.max_retries + 1):
            try:
                async with (
                    httpx.AsyncClient(
                        timeout=timeout, follow_redirects=False, transport=self._transport
                    ) as client,
                    client.stream(
                        method,
                        url,
                        headers=headers,
                        params=params or None,
                        json=json_body,
                    ) as response,
                ):
                    body, truncated = await self._read_capped(response, config.max_response_bytes)
                    status_code = response.status_code
                    response_headers = dict(response.headers)
            except httpx.TimeoutException:
                last_reason = "failed:timeout"
            except httpx.HTTPError as exc:
                last_reason = f"failed:connection_error:{type(exc).__name__}"
            else:
                if status_code in _RETRYABLE_STATUSES and attempt < config.max_retries:
                    last_reason = f"failed:retryable_status_{status_code}"
                    await asyncio.sleep(config.retry_backoff_seconds * (attempt + 1))
                    continue
                return status_code, response_headers, body, truncated

            if attempt < config.max_retries:
                await asyncio.sleep(config.retry_backoff_seconds * (attempt + 1))
        return last_reason

    @staticmethod
    async def _read_capped(response: httpx.Response, max_bytes: int) -> tuple[bytes, bool]:
        chunks: list[bytes] = []
        total = 0
        truncated = False
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > max_bytes:
                truncated = True
                break
            chunks.append(chunk)
        return b"".join(chunks), truncated
