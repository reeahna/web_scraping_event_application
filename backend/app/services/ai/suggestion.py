"""Validating an untrusted AI suggestion into a safe draft configuration.

The provider's answer is treated as hostile input. It becomes a draft only
after passing every check deterministic configuration already passes, plus a
few that exist specifically because the source is a language model:

* it must validate against the restricted `SiteConfiguration` schema, which
  already forbids executable code, env-var references, forbidden headers, and
  unknown fields (`extra="forbid"`)
* its pattern must be registered and within the caller's allowed set
* every configured URL must pass public/SSRF validation
* no request headers may be configured (the schema blocks credential headers;
  this blocks *all* headers on an AI draft, since none were human-reviewed)
* pagination and detail-fetch limits must be within bounds
* it may not carry approval, activation, or persistence instructions — there
  is simply no field for those, so an unknown key is rejected outright

Nothing here executes anything from the suggestion.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import ValidationError

from app.core.url_safety import UnsafeURLError, validate_public_url
from app.schemas.extraction import SiteConfiguration

# Hard ceilings an AI draft may never exceed, independent of policy — a
# suggestion cannot ask the engine to fetch thousands of pages.
_MAX_PAGES = 10
_MAX_EVENTS = 500
_MAX_DETAIL_FETCHES = 25


@dataclass(frozen=True)
class SuggestionValidation:
    configuration: SiteConfiguration | None
    errors: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return self.configuration is not None


def validate_suggestion(
    suggestion: dict | None,
    *,
    allowed_pattern_names: frozenset[str],
    registered_patterns: frozenset[str],
) -> SuggestionValidation:
    if not isinstance(suggestion, dict):
        return SuggestionValidation(None, ("the suggestion was not a JSON object",))

    # A suggestion may only ever be a SiteConfiguration. Anything that looks
    # like an instruction ("approve", "activate", "run") has no schema field
    # and is rejected by extra="forbid".
    try:
        configuration = SiteConfiguration.model_validate(suggestion)
    except ValidationError as exc:
        messages = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]
        return SuggestionValidation(None, tuple(messages[:12]))

    errors: list[str] = []

    if configuration.pattern_name not in registered_patterns:
        errors.append(f"pattern '{configuration.pattern_name}' is not a registered pattern")
    elif configuration.pattern_name not in allowed_pattern_names and allowed_pattern_names:
        errors.append(
            f"pattern '{configuration.pattern_name}' is outside the allowed set for this source"
        )

    for label, url in (
        ("listing_url", configuration.listing_url),
        ("api_endpoint", configuration.api_endpoint),
    ):
        if not url:
            continue
        try:
            validate_public_url(url)
        except UnsafeURLError as exc:
            errors.append(f"{label} is not a safe public URL: {exc}")

    if configuration.fetch.headers:
        errors.append("an AI suggestion may not configure request headers")
    if configuration.fetch.json_body:
        errors.append("an AI suggestion may not configure a request body")

    if configuration.pagination.max_pages > _MAX_PAGES:
        errors.append(
            f"max_pages {configuration.pagination.max_pages} exceeds the ceiling {_MAX_PAGES}"
        )
    if configuration.pagination.max_events > _MAX_EVENTS:
        errors.append(
            f"max_events {configuration.pagination.max_events} exceeds the ceiling {_MAX_EVENTS}"
        )
    if configuration.max_detail_fetches > _MAX_DETAIL_FETCHES:
        errors.append(
            f"max_detail_fetches {configuration.max_detail_fetches} exceeds the ceiling "
            f"{_MAX_DETAIL_FETCHES}"
        )

    if errors:
        return SuggestionValidation(None, tuple(errors))
    return SuggestionValidation(configuration, ())
