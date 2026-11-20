"""LLM-backed event categorization (Groq or Gemini).

A focused labeling pass that reads an event's own words (title, description,
source category, venue) and picks the single best-fitting category from the
deployment's taxonomy. It is far more accurate than the keyword rules in
app.services.categorization because it understands meaning rather than matching
words: it knows a "cooking class" or "empanada workshop" is food-and-drink, not
education, even though the word "class" is present.

This is deliberately a separate, opt-in module, not part of the always-on
import pipeline:

* It runs once per event (see scripts/categorize_events_ai.py), never on every
  page view, so a full catalog pass and the trickle of new events stay well
  inside Google AI Studio's free tier.
* Events are sent in batches and the client waits a configurable minimum
  interval between requests to respect the free-tier requests-per-minute limit.
* The whole thing degrades to "unavailable" with no key, so nothing depends on
  it — the keyword rules remain the default categorizer.

Two providers share one prompt and parser: Groq (GroqCategorizer, used when
GROQ_API_KEY is set; its free tier allows far more requests a day) and Google's
Gemini (GeminiCategorizer). The exception classes keep their Gemini names but
are raised by both. No key is ever supplied in development or tests, so no
automated code path reaches the network.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass

import httpx

from app.core.categories import CATEGORY_DESCRIPTIONS

# Bump when the prompt or category framing changes so cached labels from an
# older framing are recomputed rather than trusted. cat-v3: Groq replaced
# Gemini, whose labels were not accurate enough to keep, so every event is
# labeled again.
PROMPT_VERSION = "cat-v3"

_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
_GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"
_MAX_DESCRIPTION_CHARS = 600
# Statuses worth a retry with backoff: rate limiting and transient server / 5xx.
_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


class GeminiError(Exception):
    """A Gemini call failed (network, non-retryable status, or unparseable
    body). The caller treats a batch that errors as 'no labels for these
    events' and leaves their existing categories untouched."""


class GeminiUnavailable(GeminiError):
    """No API key is configured, so categorization cannot run at all."""


class GeminiOverloaded(GeminiError):
    """Google answered with a 5xx after retries (in practice 503 "the model is
    overloaded"). Not our fault and not a quota problem, but on the free tier
    each failed request still counts toward the daily limit, so callers back
    off rather than asking again straight away."""


class GeminiTransportError(GeminiError):
    """The request never got an answer (network failure or timeout). Says
    nothing about the events in it, so they are not charged an attempt."""


class GeminiBadOutput(GeminiError):
    """The provider answered, but not with usable labels: a body that is not
    the JSON asked for, or a 400 such as Groq's json_validate_failed. Counted
    as an unsuccessful attempt for each event in the batch."""


class GeminiQuotaExceeded(GeminiError):
    """The API returned 429 after retries: the rate or daily-quota limit is
    reached. Callers stop cleanly and resume later — the free-tier daily quota
    resets on its own, and already-labeled events are skipped on the next run.
    retry_after_seconds is how long Google says to wait, when it says."""

    def __init__(
        self,
        message: str,
        retry_after_seconds: float | None = None,
        limit_kind: str | None = None,
    ) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds
        # Which limit was hit ("TPD", "TPM", "RPD", "RPM") when the provider says.
        self.limit_kind = limit_kind


@dataclass
class CallStats:
    """What one labeling request cost and what the provider said about its
    limits, for monitoring (app.models.ai_label_batch). Filled in by every
    request, successful or not. Holds no key and no request body."""

    model: str
    http_status: int | None = None
    duration_ms: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cached_tokens: int | None = None
    reasoning_tokens: int | None = None
    limit_requests: int | None = None
    remaining_requests: int | None = None
    reset_requests: str | None = None
    limit_tokens: int | None = None
    remaining_tokens: int | None = None
    reset_tokens: str | None = None
    retry_after_seconds: float | None = None
    limit_kind: str | None = None
    limit_value: int | None = None
    limit_used: int | None = None
    limit_requested: int | None = None
    error: str | None = None


@dataclass(frozen=True)
class CategoryOption:
    slug: str
    name: str
    description: str | None = None


@dataclass(frozen=True)
class EventLabel:
    """The model's verdict on one event: its category, and whether it is
    something a college student would want to go to (keep=False for
    professional, finance, or industry events and the like)."""

    category: str
    keep: bool = True


@dataclass(frozen=True)
class EventToClassify:
    id: int
    title: str
    description: str | None = None
    source_category: str | None = None
    venue: str | None = None


def _clean(value: str | None, *, limit: int | None = None) -> str:
    text = " ".join((value or "").split())
    if limit is not None and len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def _category_lines(categories: list[CategoryOption]) -> str:
    lines = []
    for option in categories:
        # Fall back to the built-in wording so a deployment seeded before
        # descriptions existed still gets a useful prompt.
        description = _clean(
            option.description or CATEGORY_DESCRIPTIONS.get(option.slug), limit=200
        )
        suffix = f" — {description}" if description else ""
        lines.append(f"- {option.slug}: {option.name}{suffix}")
    return "\n".join(lines)


def _event_payload(
    events: list[EventToClassify], *, description_chars: int = _MAX_DESCRIPTION_CHARS
) -> list[dict[str, str | int]]:
    payload: list[dict[str, str | int]] = []
    for event in events:
        entry: dict[str, str | int] = {"id": event.id, "title": _clean(event.title, limit=200)}
        source_category = _clean(event.source_category, limit=100)
        if source_category:
            entry["source_category"] = source_category
        venue = _clean(event.venue, limit=120)
        if venue:
            entry["venue"] = venue
        description = _clean(event.description, limit=description_chars)
        if description:
            entry["description"] = description
        payload.append(entry)
    return payload


def build_instructions(categories: list[CategoryOption]) -> str:
    """The part of the prompt that is the same for every batch: the task, the
    category list and the answer format. Sent first so a provider that caches
    prompt prefixes (Groq does for gpt-oss) can reuse it between requests."""
    return (
        "You are labeling events for an app that shows college students things to "
        "do, both on campus and anywhere in their college town. For each event, "
        "pick exactly one category from the list below, choosing the slug that "
        "best matches what an attendee will actually do or experience, and decide "
        "whether to keep it.\n\n"
        "Categories:\n"
        f"{_category_lines(categories)}\n\n"
        "Choosing a category:\n"
        "- Judge by the true subject, not by a single word. A cooking class, "
        "empanada workshop, wine tasting, or brewery tour is food-and-drink, "
        "not education, even though it may be called a class. A pottery, "
        "painting, or dance class is arts-and-culture.\n"
        "- A concert or live band is music even if held at a bar or festival.\n"
        "- Use 'other' only when no category reasonably fits.\n\n"
        "Deciding keep:\n"
        "- keep is true for anything a college student might choose to go to for "
        "fun, to socialize, to try something new, or out of interest: concerts, "
        "games, parties, festivals, markets, hands-on classes (pottery, cooking, "
        "dance, yoga), public talks, club meetings, volunteering, and student "
        "career fairs. Town events count as much as campus ones.\n"
        "- keep is false for events aimed at working professionals or "
        "businesses: financial, investing, retirement, real-estate, insurance, "
        "or tax seminars; professional conferences, trade shows, and industry "
        "networking; certification, licensing, or continuing-education courses; "
        "and internal staff, faculty, or committee meetings.\n"
        "- When unsure, keep is true.\n\n"
        "Every event id below must appear exactly once in your answer. Return "
        "ONLY a JSON array, no prose, where each element is "
        '{"id": <the event id>, "category": "<a slug from the list>", '
        '"keep": true or false}.\n\n'
    )


def build_event_block(
    events: list[EventToClassify], *, description_chars: int = _MAX_DESCRIPTION_CHARS
) -> str:
    """The part of the prompt that changes with every batch."""
    payload = _event_payload(events, description_chars=description_chars)
    return f"Events:\n{json.dumps(payload, ensure_ascii=False)}"


def build_prompt(
    categories: list[CategoryOption],
    events: list[EventToClassify],
    *,
    description_chars: int = _MAX_DESCRIPTION_CHARS,
) -> str:
    return build_instructions(categories) + build_event_block(
        events, description_chars=description_chars
    )


@dataclass(frozen=True)
class SplitPrompt:
    """A prompt sent as a fixed instructions message followed by the batch's
    events, so the instructions form an identical prefix on every request."""

    instructions: str
    events: str


# How a prompt is laid out in the request. "single": everything in one user
# message (the original layout). "split": instructions in the system message,
# events in the user message, so the cacheable prefix is as long as possible.
PROMPT_LAYOUTS = ("single", "split")


def _extract_text(body: dict) -> str:
    candidates = body.get("candidates") or []
    if not candidates:
        # A prompt blocked by a safety filter has no candidates; surface it.
        feedback = body.get("promptFeedback") or {}
        raise GeminiError(f"no candidates returned (promptFeedback={feedback})")
    parts = ((candidates[0] or {}).get("content") or {}).get("parts") or []
    text = "".join(part.get("text", "") for part in parts).strip()
    if not text:
        raise GeminiError("empty response text")
    return text


def _as_label_list(data: object) -> list | None:
    """The label array out of whatever JSON shape the model chose. JSON-object
    mode (Groq) has to wrap the array, and models do it more than one way:
    {"labels": [...]}, a lone label {"id": 5, ...} when the batch holds a
    single event, or {"labels": {"5": {...}}} keyed by event id. None when no
    labels can be found."""
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return None
    if "id" in data:
        return [data]
    for value in data.values():
        if isinstance(value, list):
            return value
    for value in data.values():
        found = _as_label_list(value) if isinstance(value, dict) else None
        if found:
            return found
    # Keyed by event id: {"5": {"category": ...}, "6": {...}}.
    keyed = [
        {"id": key, **value}
        for key, value in data.items()
        if isinstance(value, dict) and str(key).strip().isdigit()
    ]
    return keyed or None


def _parse_labels(text: str) -> dict[int, EventLabel]:
    # responseMimeType=application/json makes the body a bare JSON array, but be
    # tolerant of a stray fence or surrounding prose just in case.
    snippet = text.strip()
    if snippet.startswith("```"):
        snippet = re.sub(r"^```(?:json)?|```$", "", snippet, flags=re.MULTILINE).strip()
    try:
        data = json.loads(snippet)
    except json.JSONDecodeError:
        match = re.search(r"\[.*\]", snippet, flags=re.DOTALL)
        if not match:
            raise GeminiError(f"response was not JSON: {text[:200]!r}") from None
        data = json.loads(match.group(0))
    data = _as_label_list(data)
    if data is None:
        raise GeminiError(f"response JSON was not a list: {snippet[:200]!r}")
    labels: dict[int, EventLabel] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            event_id = int(item["id"])
        except (KeyError, TypeError, ValueError):
            continue
        slug = str(item.get("category", "")).strip().lower()
        if slug:
            # Only an explicit false drops an event; a missing or odd value keeps it.
            labels[event_id] = EventLabel(category=slug, keep=item.get("keep") is not False)
    return labels


def _error_message(response: httpx.Response) -> str:
    # Google's error JSON carries the useful part (for a 429, which quota and
    # its limit) in error.message, often past the first few hundred characters
    # of the raw body.
    try:
        message = response.json()["error"]["message"]
    except (ValueError, KeyError, TypeError):
        return response.text[:200]
    return _clean(str(message), limit=500)


_SECRET_LIKE = re.compile(
    r"\b(?:org_[A-Za-z0-9]+|gsk_[A-Za-z0-9]+|sk-[A-Za-z0-9_-]+|AIza[0-9A-Za-z_-]{20,})\b"
)


def sanitize_error(text: str | None, *, limit: int = 300) -> str | None:
    """An error message safe to store and log: organization ids and anything
    shaped like an API key are masked, whitespace collapsed, length capped."""
    if not text:
        return None
    return _clean(_SECRET_LIKE.sub("[redacted]", str(text)), limit=limit)


# Groq names the limit in its 429 message: "... on tokens per day (TPD): Limit
# 200000, Used 199102, Requested 4015. Please try again in 27m41.5s ..."
_LIMIT_DETAIL = re.compile(
    r"\((?P<kind>[A-Z]{3,4})\):\s*Limit\s+(?P<limit>\d+),\s*Used\s+(?P<used>\d+)"
    r"(?:,\s*Requested\s+(?P<requested>\d+))?",
)


def parse_limit_detail(message: str | None) -> dict[str, int | str | None]:
    """{"kind", "limit", "used", "requested"} from a rate-limit message, with
    None for whatever it does not say."""
    match = _LIMIT_DETAIL.search(message or "")
    if not match:
        return {"kind": None, "limit": None, "used": None, "requested": None}
    requested = match.group("requested")
    return {
        "kind": match.group("kind"),
        "limit": int(match.group("limit")),
        "used": int(match.group("used")),
        "requested": int(requested) if requested else None,
    }


def _int_or_none(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _read_rate_headers(stats: CallStats, response: httpx.Response) -> None:
    headers = response.headers
    stats.limit_requests = _int_or_none(headers.get("x-ratelimit-limit-requests"))
    stats.remaining_requests = _int_or_none(headers.get("x-ratelimit-remaining-requests"))
    stats.reset_requests = (headers.get("x-ratelimit-reset-requests") or "")[:32] or None
    stats.limit_tokens = _int_or_none(headers.get("x-ratelimit-limit-tokens"))
    stats.remaining_tokens = _int_or_none(headers.get("x-ratelimit-remaining-tokens"))
    stats.reset_tokens = (headers.get("x-ratelimit-reset-tokens") or "")[:32] or None


def _read_usage(stats: CallStats, body: dict) -> None:
    """Token counts from an OpenAI-style "usage" or Gemini "usageMetadata"."""
    usage = body.get("usage")
    if isinstance(usage, dict):
        stats.prompt_tokens = _int_or_none(usage.get("prompt_tokens"))
        stats.completion_tokens = _int_or_none(usage.get("completion_tokens"))
        stats.total_tokens = _int_or_none(usage.get("total_tokens"))
        prompt_details = usage.get("prompt_tokens_details") or {}
        if isinstance(prompt_details, dict):
            stats.cached_tokens = _int_or_none(prompt_details.get("cached_tokens"))
        completion_details = usage.get("completion_tokens_details") or {}
        if isinstance(completion_details, dict):
            stats.reasoning_tokens = _int_or_none(completion_details.get("reasoning_tokens"))
        return
    meta = body.get("usageMetadata")
    if isinstance(meta, dict):
        stats.prompt_tokens = _int_or_none(meta.get("promptTokenCount"))
        stats.completion_tokens = _int_or_none(meta.get("candidatesTokenCount"))
        stats.total_tokens = _int_or_none(meta.get("totalTokenCount"))
        stats.cached_tokens = _int_or_none(meta.get("cachedContentTokenCount"))
        stats.reasoning_tokens = _int_or_none(meta.get("thoughtsTokenCount"))


_RETRY_IN = re.compile(r"(?:retry|try again) in ((?:\d+(?:\.\d+)?[hms])+)", re.IGNORECASE)
_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)([hms])")


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """How long a 429 says to wait. Google puts it in a RetryInfo detail
    ("retryDelay": "26487s") and in the message ("Please retry in 7h21m27s");
    Groq in a Retry-After header and the message ("Please try again in
    7m12.5s"). Any one is enough. None when none is present."""
    header = response.headers.get("retry-after")
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    try:
        body = response.json()
    except ValueError:
        body = None
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        for detail in error.get("details") or []:
            delay = detail.get("retryDelay") if isinstance(detail, dict) else None
            if isinstance(delay, str) and delay.endswith("s"):
                try:
                    return float(delay[:-1])
                except ValueError:
                    pass
        match = _RETRY_IN.search(str(error.get("message", "")))
        if match:
            scale = {"h": 3600, "m": 60, "s": 1}
            return sum(
                float(amount) * scale[unit]
                for amount, unit in _DURATION_PART.findall(match.group(1))
            )
    return None


class GeminiCategorizer:
    """Synchronous Gemini client for batch event categorization. One instance
    per run; call classify_batch() repeatedly. Self-rate-limits so the caller
    can just loop over batches without tracking timing."""

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        timeout: float = 60.0,
        min_interval_seconds: float = 4.0,
        max_retries: int = 4,
        fallback_model: str | None = None,
        description_chars: int = _MAX_DESCRIPTION_CHARS,
    ) -> None:
        if not api_key:
            raise GeminiUnavailable("no Gemini API key configured")
        self.model = model
        self.description_chars = max(0, description_chars)
        self.prompt_layout = "single"
        self._min_interval = max(0.0, min_interval_seconds)
        self._max_retries = max(0, max_retries)
        self._url = _ENDPOINT.format(model=model)
        # When the primary model is overloaded (5xx), the same request is sent
        # once to this model instead. Free-tier capacity and quota are per model.
        self._fallback_url = (
            _ENDPOINT.format(model=fallback_model)
            if fallback_model and fallback_model != model
            else None
        )
        self._last_request_at = 0.0
        self.last_stats = CallStats(model=model)
        # The key rides as a query parameter per Google AI Studio's REST API; it
        # never goes in a logged header.
        self._client = httpx.Client(timeout=timeout, params={"key": api_key})

    def __enter__(self) -> GeminiCategorizer:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _throttle(self) -> None:
        if self._min_interval <= 0:
            return
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)

    def _build_prompt(
        self, categories: list[CategoryOption], events: list[EventToClassify]
    ) -> str | SplitPrompt:
        if self.prompt_layout == "split":
            return SplitPrompt(
                build_instructions(categories),
                build_event_block(events, description_chars=self.description_chars),
            )
        return build_prompt(categories, events, description_chars=self.description_chars)

    def _payload(self, prompt: str | SplitPrompt) -> dict:
        if isinstance(prompt, SplitPrompt):
            prompt = prompt.instructions + prompt.events
        return {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.0,
                "responseMimeType": "application/json",
            },
        }

    def _response_text(self, body: dict) -> str:
        return _extract_text(body)

    def _post(self, prompt: str) -> dict:
        payload = self._payload(prompt)
        url = self._url
        attempt = 0
        stats = CallStats(model=self.model)
        self.last_stats = stats
        while True:
            self._throttle()
            self._last_request_at = time.monotonic()
            started = time.monotonic()
            try:
                response = self._client.post(url, json=payload)
            except httpx.HTTPError as exc:
                stats.duration_ms = int((time.monotonic() - started) * 1000)
                stats.error = sanitize_error(f"request failed: {type(exc).__name__}")
                if attempt >= self._max_retries:
                    raise GeminiTransportError(f"request failed: {exc}") from exc
                attempt += 1
                time.sleep(min(2**attempt, 30))
                continue
            stats.duration_ms = int((time.monotonic() - started) * 1000)
            stats.http_status = response.status_code
            _read_rate_headers(stats, response)
            if response.status_code in _RETRYABLE_STATUSES and attempt < self._max_retries:
                attempt += 1
                # Honour Retry-After when present, else exponential backoff.
                delay = response.headers.get("retry-after")
                wait = float(delay) if delay and delay.isdigit() else min(2**attempt, 60)
                time.sleep(wait)
                continue
            if response.status_code >= 500 and self._fallback_url and url != self._fallback_url:
                url = self._fallback_url
                attempt = 0
                continue
            if response.status_code != 200:
                detail = _error_message(response)
                message = f"HTTP {response.status_code}: {sanitize_error(detail, limit=500)}"
                stats.error = sanitize_error(message)
                if response.status_code == 429:
                    limit = parse_limit_detail(detail)
                    stats.retry_after_seconds = _retry_after_seconds(response)
                    stats.limit_kind = limit["kind"]  # type: ignore[assignment]
                    stats.limit_value = limit["limit"]  # type: ignore[assignment]
                    stats.limit_used = limit["used"]  # type: ignore[assignment]
                    stats.limit_requested = limit["requested"]  # type: ignore[assignment]
                    raise GeminiQuotaExceeded(
                        message, stats.retry_after_seconds, stats.limit_kind
                    )
                if response.status_code >= 500:
                    raise GeminiOverloaded(message)
                if response.status_code == 400:
                    # A request the model could not answer in the asked-for
                    # shape (Groq: json_validate_failed). Configuration errors
                    # (401, 403, 404, 413) are not the events' fault.
                    raise GeminiBadOutput(message)
                raise GeminiError(message)
            try:
                body = response.json()
            except ValueError as exc:
                stats.error = "response body was not JSON"
                raise GeminiBadOutput("response body was not JSON") from exc
            _read_usage(stats, body)
            return body

    def classify_batch(
        self, categories: list[CategoryOption], events: list[EventToClassify]
    ) -> dict[int, EventLabel]:
        """Return {event_id: EventLabel} for the given events. Only slugs
        that exist in `categories` are returned; anything the model invents is
        dropped so the caller can fall back to its own default. Events the model
        omits are simply absent from the result."""
        if not events:
            return {}
        valid = {option.slug for option in categories}
        prompt = self._build_prompt(categories, events)
        body = self._post(prompt)
        try:
            labels = _parse_labels(self._response_text(body))
        except GeminiError as exc:
            self.last_stats.error = sanitize_error(str(exc))
            raise GeminiBadOutput(str(exc)) from exc
        except ValueError as exc:  # json.JSONDecodeError from the fallback parse
            self.last_stats.error = sanitize_error(f"unparseable labels: {exc}")
            raise GeminiBadOutput(f"unparseable labels: {exc}") from exc
        requested = {event.id for event in events}
        return {
            event_id: label
            for event_id, label in labels.items()
            if event_id in requested and label.category in valid
        }


class GroqCategorizer(GeminiCategorizer):
    """The same batch categorizer against Groq's OpenAI-compatible API. Groq's
    free tier allows far more requests a day than Gemini's (around 1,000 for
    gpt-oss-120b at the time of writing) but caps tokens per minute, so
    batches are smaller and requests further apart."""

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        timeout: float = 60.0,
        min_interval_seconds: float = 4.0,
        max_retries: int = 4,
        description_chars: int = _MAX_DESCRIPTION_CHARS,
        prompt_layout: str = "single",
    ) -> None:
        if not api_key:
            raise GeminiUnavailable("no Groq API key configured")
        if prompt_layout not in PROMPT_LAYOUTS:
            raise ValueError(f"unknown prompt layout {prompt_layout!r}")
        self.model = model
        self.description_chars = max(0, description_chars)
        self.prompt_layout = prompt_layout
        self._min_interval = max(0.0, min_interval_seconds)
        self._max_retries = max(0, max_retries)
        self._url = _GROQ_ENDPOINT
        self._fallback_url = None
        self._last_request_at = 0.0
        self.last_stats = CallStats(model=model)
        self._client = httpx.Client(
            timeout=timeout, headers={"Authorization": f"Bearer {api_key}"}
        )

    def __enter__(self) -> GroqCategorizer:
        return self

    def _payload(self, prompt: str | SplitPrompt) -> dict:
        wrap = (
            'Reply with a JSON object {"labels": [...]} whose '
            "labels array is exactly the array the user asks for."
        )
        if isinstance(prompt, SplitPrompt):
            # Everything that never changes goes first, in the system message,
            # so consecutive requests share the longest possible prefix.
            messages = [
                {"role": "system", "content": prompt.instructions + wrap},
                {"role": "user", "content": prompt.events},
            ]
        else:
            messages = [
                {"role": "system", "content": wrap},
                {"role": "user", "content": prompt},
            ]
        payload = {
            "model": self.model,
            "temperature": 0.0,
            # JSON mode returns an object, so the array is wrapped in "labels".
            "response_format": {"type": "json_object"},
            "messages": messages,
        }
        if self.model.startswith("openai/gpt-oss"):
            # Its reasoning tokens count against the per-minute token limit and
            # labeling needs little of it.
            payload["reasoning_effort"] = "low"
        return payload

    def _response_text(self, body: dict) -> str:
        choices = body.get("choices") or []
        text = ((choices[0] or {}).get("message") or {}).get("content") if choices else None
        if not text or not str(text).strip():
            raise GeminiError("empty response text")
        return str(text)
