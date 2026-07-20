"""Translates the structured configuration-review form into a
SiteConfiguration. Simple/scalar fields come from real form inputs; the
inherently list/dict-shaped pieces (field_selectors, json_paths,
transformations, exclusion_rules, category_mappings, geographic_filters)
each get their own small, independently-validated JSON sub-editor — never
one opaque blob for the whole config. Structured errors are returned keyed
by section name so the template can show each error next to its own field,
not as one generic message.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from app.schemas.extraction import SiteConfiguration

# Fields commonly required across patterns; rendered as checkboxes rather
# than a free-text list.
REQUIRED_FIELD_CHOICES: tuple[str, ...] = (
    "title",
    "start_date",
    "canonical_url",
    "venue",
    "address",
    "description",
    "image_url",
)

_SCOPED_JSON_FIELDS: tuple[str, ...] = (
    "field_selectors",
    "json_paths",
    "transformations",
    "category_mappings",
    "exclusion_rules",
    "geographic_filters",
)


@dataclass
class ConfigFormInput:
    pattern_name: str
    listing_url: str = ""
    api_endpoint: str = ""
    timezone: str = ""
    event_container_selector: str = ""
    detail_page_selector: str = ""
    max_detail_fetches: str = "25"
    pagination_strategy: str = "none"
    page_param: str = ""
    page_size_param: str = ""
    next_page_selector: str = ""
    max_pages: str = "10"
    max_events: str = "500"
    date_formats: str = ""
    time_formats: str = ""
    required_fields: list[str] = field(default_factory=list)
    allow_page_url_as_canonical_fallback: bool = False
    allow_offers_url_as_event_url: bool = False
    field_selectors: str = ""
    json_paths: str = ""
    transformations: str = ""
    category_mappings: str = ""
    exclusion_rules: str = ""
    geographic_filters: str = ""
    raw_json: str = ""


@dataclass
class ConfigFormResult:
    configuration: SiteConfiguration | None
    errors: dict[str, str]


def _parse_scoped_json(raw: str, *, default: Any) -> tuple[Any, str | None]:
    raw = raw.strip()
    if not raw:
        return default, None
    try:
        return json.loads(raw), None
    except json.JSONDecodeError as exc:
        return default, f"Must be valid JSON: {exc}"


def _parse_lines(raw: str) -> list[str]:
    return [line.strip() for line in raw.splitlines() if line.strip()]


def build_site_configuration(form: ConfigFormInput) -> ConfigFormResult:
    """Returns either a validated SiteConfiguration or a dict of
    section-scoped error messages — never both, and never a partially
    valid configuration."""
    if form.raw_json.strip():
        try:
            payload = json.loads(form.raw_json)
        except json.JSONDecodeError as exc:
            return ConfigFormResult(None, {"raw_json": f"Must be valid JSON: {exc}"})
        try:
            return ConfigFormResult(SiteConfiguration.model_validate(payload), {})
        except ValidationError as exc:
            return ConfigFormResult(None, {"raw_json": str(exc)})

    errors: dict[str, str] = {}
    scoped_defaults: dict[str, Any] = {
        "field_selectors": {},
        "json_paths": {},
        "transformations": [],
        "category_mappings": {},
        "exclusion_rules": [],
        "geographic_filters": None,
    }
    parsed_scoped: dict[str, Any] = {}
    for name in _SCOPED_JSON_FIELDS:
        value, error = _parse_scoped_json(getattr(form, name), default=scoped_defaults[name])
        parsed_scoped[name] = value
        if error:
            errors[name] = error

    try:
        max_detail_fetches = int(form.max_detail_fetches or 25)
        max_pages = int(form.max_pages or 10)
        max_events = int(form.max_events or 500)
    except ValueError:
        errors["pagination"] = "Numeric fields must be whole numbers."
        max_detail_fetches, max_pages, max_events = 25, 10, 500

    if errors:
        return ConfigFormResult(None, errors)

    kwargs: dict[str, Any] = {
        "pattern_name": form.pattern_name,
        "listing_url": form.listing_url or None,
        "api_endpoint": form.api_endpoint or None,
        "timezone": form.timezone or None,
        "event_container_selector": form.event_container_selector or None,
        "detail_page_selector": form.detail_page_selector or None,
        "max_detail_fetches": max_detail_fetches,
        "pagination": {
            "strategy": form.pagination_strategy,
            "page_param": form.page_param or None,
            "page_size_param": form.page_size_param or None,
            "next_page_selector": form.next_page_selector or None,
            "max_pages": max_pages,
            "max_events": max_events,
        },
        "date_formats": _parse_lines(form.date_formats),
        "time_formats": _parse_lines(form.time_formats),
        "required_fields": form.required_fields or ["title", "start_date", "canonical_url"],
        "allow_page_url_as_canonical_fallback": form.allow_page_url_as_canonical_fallback,
        "allow_offers_url_as_event_url": form.allow_offers_url_as_event_url,
        **parsed_scoped,
    }

    try:
        return ConfigFormResult(SiteConfiguration.model_validate(kwargs), {})
    except ValidationError as exc:
        for err in exc.errors():
            field_name = str(err["loc"][0]) if err["loc"] else "configuration"
            errors[field_name] = err["msg"]
        return ConfigFormResult(None, errors)


def configuration_to_form(config: SiteConfiguration) -> ConfigFormInput:
    data = config.model_dump(mode="json")
    pagination = data.get("pagination") or {}
    return ConfigFormInput(
        pattern_name=data["pattern_name"],
        listing_url=data.get("listing_url") or "",
        api_endpoint=data.get("api_endpoint") or "",
        timezone=data.get("timezone") or "",
        event_container_selector=data.get("event_container_selector") or "",
        detail_page_selector=data.get("detail_page_selector") or "",
        max_detail_fetches=str(data.get("max_detail_fetches", 25)),
        pagination_strategy=pagination.get("strategy", "none"),
        page_param=pagination.get("page_param") or "",
        page_size_param=pagination.get("page_size_param") or "",
        next_page_selector=pagination.get("next_page_selector") or "",
        max_pages=str(pagination.get("max_pages", 10)),
        max_events=str(pagination.get("max_events", 500)),
        date_formats="\n".join(data.get("date_formats") or []),
        time_formats="\n".join(data.get("time_formats") or []),
        required_fields=data.get("required_fields") or [],
        allow_page_url_as_canonical_fallback=bool(data.get("allow_page_url_as_canonical_fallback")),
        allow_offers_url_as_event_url=bool(data.get("allow_offers_url_as_event_url")),
        field_selectors=json.dumps(data.get("field_selectors") or {}, indent=2),
        json_paths=json.dumps(data.get("json_paths") or {}, indent=2),
        transformations=json.dumps(data.get("transformations") or [], indent=2),
        category_mappings=json.dumps(data.get("category_mappings") or {}, indent=2),
        exclusion_rules=json.dumps(data.get("exclusion_rules") or [], indent=2),
        geographic_filters=json.dumps(data.get("geographic_filters"))
        if data.get("geographic_filters")
        else "",
    )
