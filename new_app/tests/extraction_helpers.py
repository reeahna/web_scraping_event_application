"""Shared helpers for extraction-engine tests. Not a pytest fixture module —
just plain functions imported directly by tests/test_extraction_*.py.
"""

import hashlib
import ipaddress
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from app.extraction.fetch import HttpFetchStrategy
from app.extraction.types import FetchResponse

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "extraction"


def load_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def make_response(
    body: str | bytes,
    *,
    final_url: str = "https://example.com/events",
    status_code: int = 200,
    content_type: str = "text/html",
    headers: dict[str, str] | None = None,
    blocked_reason: str | None = None,
    redirect_history: tuple[str, ...] = (),
) -> FetchResponse:
    body_bytes = body.encode("utf-8") if isinstance(body, str) else body
    merged_headers = {"content-type": content_type, **(headers or {})}
    return FetchResponse(
        request_url=final_url,
        final_url=final_url,
        status_code=status_code,
        headers=merged_headers,
        content_type=content_type,
        body=body_bytes,
        redirect_history=redirect_history,
        body_hash=hashlib.sha256(body_bytes).hexdigest(),
        elapsed_seconds=0.01,
        blocked_reason=blocked_reason,
    )


def make_response_from_fixture(
    filename: str,
    *,
    final_url: str = "https://example.com/events",
    content_type: str = "text/html",
    **kwargs,
) -> FetchResponse:
    text = load_fixture(filename)
    return make_response(text, final_url=final_url, content_type=content_type, **kwargs)


@contextmanager
def patched_http_fetch(handler):
    """Patches both the DNS-resolution SSRF check (to a fixed public IP) and
    app.services.extraction_runs's HttpFetchStrategy construction (to use an
    httpx.MockTransport running `handler`) — every fetch a service-layer or
    router-level test triggers goes through `handler`, zero live network
    calls, while the real fetch/redirect/pipeline code still runs."""
    with (
        patch(
            "app.extraction.fetch.resolve_and_validate_host",
            new=AsyncMock(return_value=[ipaddress.ip_address("93.184.216.34")]),
        ),
        patch(
            "app.services.extraction_runs.HttpFetchStrategy",
            lambda: HttpFetchStrategy(transport=httpx.MockTransport(handler)),
        ),
    ):
        yield


def html_handler(fixture_name: str, content_type: str = "text/html"):
    body = load_fixture(fixture_name)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body, headers={"content-type": content_type})

    return handler


def blocked_handler(status_code: int = 403, text: str = "Access Denied"):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, text=text)

    return handler
