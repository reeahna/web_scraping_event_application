"""A closed, serializable *request recipe* for structured event endpoints.

Some providers (Simpleview-style DMO APIs and similar) only return events when
the request carries the exact shape the site's own frontend sends: a nested
JSON query parameter with filters/date-range/fields/sort/pagination, a public
site token, and a Referer pointing at the events page. Preserving only the base
endpoint yields HTTP 403. A `RequestRecipe` captures the *complete reusable*
request as plain, validated data so preview and import can reproduce it.

Everything here is plain data — `extra="forbid"`, no code, no file paths, no
environment-variable references. Absolute capture-time dates are never stored;
date boundaries and pagination cursors are dynamic placeholders resolved at
request time (see app.extraction.request_recipe). Nothing here is
provider-specific: parameter names, header names, and the JSON shape are all
captured from the observed request, never hardcoded.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from app.core.url_safety import UnsafeURLError, validate_public_url
from app.schemas.http_safety import FORBIDDEN_HEADERS, reject_env_var_reference

# Placeholder kinds that may appear inside a `json_template` value (as a bare
# `{"kind": ...}` object at any position) and are substituted at render time.
DATE_PLACEHOLDER_KINDS = frozenset({"window_start_utc", "window_end_utc"})
PAGE_PLACEHOLDER_KINDS = frozenset({"page_limit", "page_offset", "page_number"})
TEMPLATE_PLACEHOLDER_KINDS = DATE_PLACEHOLDER_KINDS | PAGE_PLACEHOLDER_KINDS | {"source_page_url"}


def _validate_json_data(value: Any, *, label: str) -> Any:
    """Rejects non-JSON-plain data and env-var references anywhere inside a
    template/literal value. Placeholder objects (`{"kind": ...}`) are allowed."""
    if isinstance(value, str):
        return reject_env_var_reference(value, field_label=label)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [_validate_json_data(item, label=label) for item in value]
    if isinstance(value, dict):
        return {str(k): _validate_json_data(v, label=label) for k, v in value.items()}
    raise ValueError(f"{label} must be plain JSON data")


class RecipeValue(BaseModel):
    """One value in a query parameter, header, or body.

    - literal: a plain scalar/list/object sent verbatim.
    - json_template: a JSON structure that may embed placeholder objects
      (`{"kind": "window_start_utc"}`, `{"kind": "page_offset"}`, …) which are
      resolved at render time and then JSON-encoded into the parameter value.
    - source_page_url: resolves to the source events page URL (e.g. Referer).
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["literal", "json_template", "source_page_url"]
    value: Any = None

    @model_validator(mode="after")
    def _validate_value(self) -> RecipeValue:
        if self.kind in ("literal", "json_template"):
            object.__setattr__(self, "value", _validate_json_data(self.value, label=self.kind))
        elif self.kind == "source_page_url":
            object.__setattr__(self, "value", None)
        return self


class RecipeWindow(BaseModel):
    """The forward date window a rendered request asks for. Only the *shape* is
    stored (a horizon in days, optionally floored to the start of the day in the
    site timezone); the absolute boundaries are computed fresh each request."""

    model_config = ConfigDict(extra="forbid")

    horizon_days: int = 30
    start_of_day: bool = True

    @field_validator("horizon_days")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v < 1 or v > 400:
            raise ValueError("horizon_days must be between 1 and 400")
        return v


class RecipePagination(BaseModel):
    """How to walk pages. `offset`/`page` re-render the recipe with the next
    cursor (via the page placeholders inside the query template); `total_path`
    is a dotted path into the response JSON to a total-count used as a stop
    condition. Every axis is bounded."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["none", "offset", "page"] = "none"
    limit: int = 100
    max_pages: int = 20
    total_path: str | None = None

    @field_validator("limit")
    @classmethod
    def _limit_bounds(cls, v: int) -> int:
        if v < 1 or v > 1000:
            raise ValueError("limit must be between 1 and 1000")
        return v

    @field_validator("max_pages")
    @classmethod
    def _pages_bounds(cls, v: int) -> int:
        if v < 1 or v > 100:
            raise ValueError("max_pages must be between 1 and 100")
        return v


class RequestRecipe(BaseModel):
    """The complete reusable request for a structured event endpoint."""

    model_config = ConfigDict(extra="forbid")

    method: Literal["GET", "POST"] = "GET"
    endpoint: str
    query_params: dict[str, RecipeValue] = {}
    headers: dict[str, RecipeValue] = {}
    body: RecipeValue | None = None
    content_type: str | None = None
    source_page_url: str | None = None
    window: RecipeWindow = RecipeWindow()
    pagination: RecipePagination = RecipePagination()

    @field_validator("endpoint", "source_page_url")
    @classmethod
    def _validate_url(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        try:
            return validate_public_url(v)
        except UnsafeURLError as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("headers")
    @classmethod
    def _validate_headers(cls, headers: dict[str, RecipeValue]) -> dict[str, RecipeValue]:
        for name in headers:
            if name.lower() in FORBIDDEN_HEADERS:
                raise ValueError(f"header '{name}' may not be set via a request recipe")
        return headers

    @model_validator(mode="after")
    def _endpoint_required(self) -> RequestRecipe:
        if not self.endpoint:
            raise ValueError("a request recipe requires an endpoint")
        return self
