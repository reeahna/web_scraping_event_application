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
# older framing are recomputed rather than trusted.
PROMPT_VERSION = "cat-v2"

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


class GeminiQuotaExceeded(GeminiError):
    """The API returned 429 after retries: the rate or daily-quota limit is
    reached. Callers stop cleanly and resume later — the free-tier daily quota
    resets on its own, and already-labeled events are skipped on the next run.
    retry_after_seconds is how long Google says to wait, when it says."""

    def __init__(self, message: str, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


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


def _event_payload(events: list[EventToClassify]) -> list[dict[str, str | int]]:
    payload: list[dict[str, str | int]] = []
    for event in events:
        entry: dict[str, str | int] = {"id": event.id, "title": _clean(event.title, limit=200)}
        source_category = _clean(event.source_category, limit=100)
        if source_category:
            entry["source_category"] = source_category
        venue = _clean(event.venue, limit=120)
        if venue:
            entry["venue"] = venue
        description = _clean(event.description, limit=_MAX_DESCRIPTION_CHARS)
        if description:
            entry["description"] = description
        payload.append(entry)
    return payload


def build_prompt(categories: list[CategoryOption], events: list[EventToClassify]) -> str:
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
        "Events:\n"
        f"{json.dumps(_event_payload(events), ensure_ascii=False)}"
    )


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
    if isinstance(data, dict):
        # JSON-object mode (Groq) wraps the array: {"labels": [...]}.
        data = next((value for value in data.values() if isinstance(value, list)), None)
    if not isinstance(data, list):
        raise GeminiError("response JSON was not a list")
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
    ) -> None:
        if not api_key:
            raise GeminiUnavailable("no Gemini API key configured")
        self.model = model
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

    def _payload(self, prompt: str) -> dict:
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
        while True:
            self._throttle()
            self._last_request_at = time.monotonic()
            try:
                response = self._client.post(url, json=payload)
            except httpx.HTTPError as exc:
                if attempt >= self._max_retries:
                    raise GeminiError(f"request failed: {exc}") from exc
                attempt += 1
                time.sleep(min(2**attempt, 30))
                continue
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
                message = f"HTTP {response.status_code}: {_error_message(response)}"
                if response.status_code == 429:
                    raise GeminiQuotaExceeded(message, _retry_after_seconds(response))
                if response.status_code >= 500:
                    raise GeminiOverloaded(message)
                raise GeminiError(message)
            return response.json()

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
        prompt = build_prompt(categories, events)
        body = self._post(prompt)
        labels = _parse_labels(self._response_text(body))
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
    ) -> None:
        if not api_key:
            raise GeminiUnavailable("no Groq API key configured")
        self.model = model
        self._min_interval = max(0.0, min_interval_seconds)
        self._max_retries = max(0, max_retries)
        self._url = _GROQ_ENDPOINT
        self._fallback_url = None
        self._last_request_at = 0.0
        self._client = httpx.Client(
            timeout=timeout, headers={"Authorization": f"Bearer {api_key}"}
        )

    def __enter__(self) -> GroqCategorizer:
        return self

    def _payload(self, prompt: str) -> dict:
        payload = {
            "model": self.model,
            "temperature": 0.0,
            # JSON mode returns an object, so the array is wrapped in "labels".
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": 'Reply with a JSON object {"labels": [...]} whose '
                    "labels array is exactly the array the user asks for.",
                },
                {"role": "user", "content": prompt},
            ],
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
