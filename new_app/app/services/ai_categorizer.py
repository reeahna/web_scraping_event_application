"""Gemini-backed event categorization.

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

Only the Google AI Studio API key is required. No key is ever supplied in
development or tests, so no automated code path reaches the network.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass

import httpx

# Bump when the prompt or category framing changes so cached labels from an
# older framing are recomputed rather than trusted.
PROMPT_VERSION = "cat-v1"

_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
_MAX_DESCRIPTION_CHARS = 400
# Statuses worth a retry with backoff: rate limiting and transient server / 5xx.
_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


class GeminiError(Exception):
    """A Gemini call failed (network, non-retryable status, or unparseable
    body). The caller treats a batch that errors as 'no labels for these
    events' and leaves their existing categories untouched."""


class GeminiUnavailable(GeminiError):
    """No API key is configured, so categorization cannot run at all."""


@dataclass(frozen=True)
class CategoryOption:
    slug: str
    name: str
    description: str | None = None


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
        description = _clean(option.description, limit=200)
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
        "You are categorizing local community events. Assign each event to "
        "exactly one category from the list below, choosing the slug that best "
        "matches what an attendee will actually do or experience.\n\n"
        "Categories:\n"
        f"{_category_lines(categories)}\n\n"
        "Guidance:\n"
        "- Judge by the true subject, not by a single word. A cooking class, "
        "empanada workshop, wine tasting, or brewery tour is food-and-drink, "
        "not education, even though it may be called a class.\n"
        "- A concert or live band is music even if held at a bar or festival.\n"
        "- Use 'other' only when no category reasonably fits.\n"
        "- Every event id below must appear exactly once in your answer.\n\n"
        "Return ONLY a JSON array, no prose, where each element is "
        '{"id": <the event id>, "category": "<a slug from the list>"}.\n\n'
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


def _parse_labels(text: str) -> dict[int, str]:
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
    if not isinstance(data, list):
        raise GeminiError("response JSON was not a list")
    labels: dict[int, str] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            event_id = int(item["id"])
        except (KeyError, TypeError, ValueError):
            continue
        slug = str(item.get("category", "")).strip().lower()
        if slug:
            labels[event_id] = slug
    return labels


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
    ) -> None:
        if not api_key:
            raise GeminiUnavailable("no Gemini API key configured")
        self.model = model
        self._min_interval = max(0.0, min_interval_seconds)
        self._max_retries = max(0, max_retries)
        self._url = _ENDPOINT.format(model=model)
        self._last_request_at = 0.0
        # The key rides as a query parameter per Google AI Studio's REST API; it
        # never goes in a logged header.
        self._client = httpx.Client(timeout=timeout, params={"key": api_key})

    def __enter__(self) -> "GeminiCategorizer":
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

    def _post(self, prompt: str) -> dict:
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.0,
                "responseMimeType": "application/json",
            },
        }
        attempt = 0
        while True:
            self._throttle()
            self._last_request_at = time.monotonic()
            try:
                response = self._client.post(self._url, json=payload)
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
            if response.status_code != 200:
                raise GeminiError(f"HTTP {response.status_code}: {response.text[:200]}")
            return response.json()

    def classify_batch(
        self, categories: list[CategoryOption], events: list[EventToClassify]
    ) -> dict[int, str]:
        """Return {event_id: category_slug} for the given events. Only slugs
        that exist in `categories` are returned; anything the model invents is
        dropped so the caller can fall back to its own default. Events the model
        omits are simply absent from the result."""
        if not events:
            return {}
        valid = {option.slug for option in categories}
        prompt = build_prompt(categories, events)
        body = self._post(prompt)
        labels = _parse_labels(_extract_text(body))
        requested = {event.id for event in events}
        return {
            event_id: slug
            for event_id, slug in labels.items()
            if event_id in requested and slug in valid
        }
