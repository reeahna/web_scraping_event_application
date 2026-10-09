"""Closed action schema for the restricted browser fetch strategy.

A browser plan is *data*, not code. There is no field anywhere here that
accepts a Python or JavaScript snippet, an arbitrary evaluate expression, a
file path, or a shell command — only a bounded, enumerated set of navigation
and interaction actions. Every count and timeout is capped by a validator, so
a plan cannot ask the browser to scroll forever or wait indefinitely.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

# Hard ceilings. A plan may configure a smaller value but never a larger one.
_MAX_TIMEOUT_MS = 15_000
_MAX_CLICKS = 10
_MAX_SCROLLS = 20
_MAX_ACTIONS = 20
_MAX_TOTAL_MS = 45_000


class _Action(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WaitForSelectorAction(_Action):
    action: Literal["wait_for_selector"] = "wait_for_selector"
    selector: str = Field(min_length=1, max_length=500)
    timeout_ms: int = Field(default=10_000, ge=100, le=_MAX_TIMEOUT_MS)


class NetworkIdleAction(_Action):
    action: Literal["network_idle"] = "network_idle"
    timeout_ms: int = Field(default=10_000, ge=100, le=_MAX_TIMEOUT_MS)


class ClickAction(_Action):
    action: Literal["click"] = "click"
    selector: str = Field(min_length=1, max_length=500)
    timeout_ms: int = Field(default=5_000, ge=100, le=_MAX_TIMEOUT_MS)


class LoadMoreAction(_Action):
    """Click a 'load more' control up to `max_clicks` times, stopping early
    when it disappears. Bounded — never an infinite loop."""

    action: Literal["load_more"] = "load_more"
    selector: str = Field(min_length=1, max_length=500)
    max_clicks: int = Field(default=3, ge=1, le=_MAX_CLICKS)
    settle_ms: int = Field(default=1_000, ge=100, le=5_000)


class ScrollAction(_Action):
    action: Literal["scroll"] = "scroll"
    max_scrolls: int = Field(default=5, ge=1, le=_MAX_SCROLLS)
    delay_ms: int = Field(default=500, ge=100, le=2_000)


class DismissBannerAction(_Action):
    """Click the first matching cookie/consent-banner control if present.
    Purely dismissal — it never accepts terms or grants consent beyond
    closing the overlay so the page content is reachable."""

    action: Literal["dismiss_banner"] = "dismiss_banner"
    selectors: list[str] = Field(default_factory=list, max_length=10)


BrowserActionType = Annotated[
    Union[  # noqa: UP007 - Annotated discriminated union needs Union form
        WaitForSelectorAction,
        NetworkIdleAction,
        ClickAction,
        LoadMoreAction,
        ScrollAction,
        DismissBannerAction,
    ],
    Field(discriminator="action"),
]


DEFAULT_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)


class BrowserPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actions: list[BrowserActionType] = Field(default_factory=list, max_length=_MAX_ACTIONS)
    max_total_ms: int = Field(default=_MAX_TOTAL_MS, ge=1_000, le=_MAX_TOTAL_MS)
    # Whether to record JSON network responses seen while the plan runs — the
    # preferred outcome, since a reusable API beats scraping rendered HTML.
    capture_json: bool = True
    # Block image/font/media subrequests to keep a render fast and cheap. On by
    # default; a plan can turn it off if a pattern genuinely needs images.
    block_media: bool = True
    # Present a normal desktop-Chrome User-Agent. Playwright's default headless
    # UA contains "HeadlessChrome", which edge/bot protection (Akamai, etc.)
    # rejects outright; a realistic UA lets the browser read the same public
    # pages a normal visitor sees. Not site-specific — applies to every
    # browser-backed source.
    user_agent: str = Field(default=DEFAULT_BROWSER_USER_AGENT, min_length=1)


def default_plan() -> BrowserPlan:
    """A conservative default: wait for the network to settle, then capture.
    Enough to let a client-rendered list paint without any site knowledge."""
    return BrowserPlan(actions=[NetworkIdleAction()])
