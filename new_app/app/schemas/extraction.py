"""Validated shape of extraction site configuration.

Stored as JSON in `Website.configuration` (the admin's editable draft) and,
as a frozen snapshot, inside `Website.approved_pattern` once approved.
`extra="forbid"` everywhere — imported/edited configuration with unknown
fields is rejected outright, never silently ignored.

Every field here is closed, plain data: no field accepts a Python/JS/shell
snippet, a local file path, or an environment-variable reference. The only
"transformation" mechanism is a closed `Literal` kind + a plain-data params
dict (see TransformationRuleConfig) — there is no way to store executable
code in a SiteConfiguration.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from app.core.url_safety import UnsafeURLError, validate_public_url
from app.schemas.geographic import GeographicFilterConfig
from app.schemas.http_safety import (  # noqa: F401 - re-exported for back-compat
    _ENV_VAR_REFERENCE_RE,
    FORBIDDEN_HEADERS,
)
from app.schemas.http_safety import (
    reject_env_var_reference as _reject_env_var_reference,
)
from app.schemas.recurrence import RecurrenceBounds, RecurrenceMode
from app.schemas.request_recipe import RequestRecipe


class FetchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: Literal["GET", "POST"] = "GET"
    headers: dict[str, str] = {}
    # Header name -> secret *reference* (e.g. "env:ALGOLIA_SEARCH_KEY"), never a
    # raw secret. The value is resolved from the environment at request time
    # (app.services.secrets) and added to the outbound headers; it is never
    # stored here, logged, or written to any audit/provenance record. This is
    # how a pattern that needs an API key (Algolia) authenticates without a
    # credential ever living in the configuration.
    secret_header_refs: dict[str, str] = {}
    query_params: dict[str, str] = {}
    json_body: dict[str, Any] | None = None
    timeout_seconds: float = 15.0
    connect_timeout_seconds: float = 5.0
    read_timeout_seconds: float = 15.0
    max_redirects: int = 5
    max_response_bytes: int = 5_000_000
    allowed_content_types: tuple[str, ...] = (
        "text/html",
        "application/json",
        "application/ld+json",
    )
    max_retries: int = 2
    retry_backoff_seconds: float = 1.0
    rate_limit_delay_seconds: float = 0.5

    @field_validator("headers")
    @classmethod
    def _validate_headers(cls, v: dict[str, str]) -> dict[str, str]:
        for key, value in v.items():
            if key.strip().lower() in FORBIDDEN_HEADERS:
                raise ValueError(f"Header '{key}' cannot be set via site configuration")
            _reject_env_var_reference(value, field_label=f"Header '{key}'")
        return v

    @field_validator("secret_header_refs")
    @classmethod
    def _validate_secret_header_refs(cls, v: dict[str, str]) -> dict[str, str]:
        # Import here to avoid a schema->service import cycle.
        from app.services.secrets import is_secret_ref

        for key, ref in v.items():
            if key.strip().lower() in FORBIDDEN_HEADERS:
                raise ValueError(f"Header '{key}' cannot be set via site configuration")
            if not is_secret_ref(ref):
                # A raw secret here would be exactly the leak this field
                # exists to prevent.
                raise ValueError(
                    f"secret_header_refs['{key}'] must be a reference like 'env:NAME', "
                    "never a literal value"
                )
        return v

    @field_validator("query_params")
    @classmethod
    def _validate_query_params(cls, v: dict[str, str]) -> dict[str, str]:
        for key, value in v.items():
            _reject_env_var_reference(value, field_label=f"Query parameter '{key}'")
        return v

    @field_validator("json_body")
    @classmethod
    def _validate_json_body(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        if v is None:
            return v

        def _walk(node: Any) -> None:
            if isinstance(node, str):
                _reject_env_var_reference(node, field_label="json_body value")
            elif isinstance(node, dict):
                for value in node.values():
                    _walk(value)
            elif isinstance(node, list):
                for value in node:
                    _walk(value)

        _walk(v)
        return v

    @model_validator(mode="after")
    def _method_requires_json_endpoint(self) -> "FetchConfig":
        if self.method == "POST" and self.json_body is None and not self.query_params:
            # POST is only meaningful against a structured endpoint that
            # expects a body or query params — never a bare listing page.
            raise ValueError("POST requires a json_body or query_params")
        return self


class PaginationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: Literal[
        "none", "query_param", "wordpress", "next_link", "tribe_rest", "livewhale_offset"
    ] = "none"
    page_param: str | None = None
    page_size_param: str | None = None
    next_page_selector: str | None = None
    max_pages: int = 10
    max_events: int = 500

    @field_validator("max_pages", "max_events")
    @classmethod
    def _validate_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("must be at least 1")
        return v


class FieldSelectorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["css", "json_path", "attribute"]
    selector: str
    attribute: str | None = None

    @field_validator("selector")
    @classmethod
    def _validate_selector_length(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("selector is required")
        if len(v) > 500:
            raise ValueError("selector must be 500 characters or fewer")
        return v


class TransformationRuleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str
    kind: Literal[
        "trim",
        "collapse_whitespace",
        "strip_html",
        "unicode_normalize",
        "prepend",
        "append",
        "parse_date",
        "parse_time",
        "relative_to_absolute_url",
        "regex_extract_group",
        "literal_replace",
        "exact_value_map",
        "lower",
        "upper",
    ]
    params: dict[str, Any] = {}


class RecurrenceRuntimeConfig(BaseModel):
    """How a source's recurrence should be handled at extraction time. The
    per-event RRULE/occurrence data comes from the pattern (in the candidate's
    raw payload); this chooses the *mode* and the expansion bounds, so nothing
    expands unless an administrator/proposer opted in. Default is parent_only,
    which preserves the existing one-row-per-event behaviour exactly."""

    model_config = ConfigDict(extra="forbid")

    mode: RecurrenceMode = "parent_only"
    bounds: RecurrenceBounds = RecurrenceBounds()


class SiteConfiguration(BaseModel):
    """Everything an extraction pattern needs to run against one website,
    independent of which registered pattern it is. Pattern-specific fields
    (e.g. `event_container_selector` for generic_html_cards, `api_endpoint`
    for wordpress_rest) simply go unused by patterns that don't need them."""

    model_config = ConfigDict(extra="forbid")

    config_version: int = 1
    pattern_name: str
    listing_url: str | None = None
    api_endpoint: str | None = None
    # Which transport executes this configuration. "http" is the ordinary
    # normalized HTTP request (default for every ordinary source). "browser"
    # renders the source page in the restricted headless browser first — either
    # extracting the rendered HTML, or capturing the page's own JSON response to
    # a configured endpoint — for sources whose events only exist after
    # client-side rendering or whose data API is edge/WAF-protected. This is an
    # explicit, stored decision (set by recovery), never inferred at runtime
    # from warning strings. No site-specific logic depends on it.
    execution_strategy: Literal["http", "browser"] = "http"
    fetch: FetchConfig = FetchConfig()
    pagination: PaginationConfig = PaginationConfig()
    timezone: str | None = None
    event_container_selector: str | None = None
    detail_page_selector: str | None = None
    max_detail_fetches: int = 25
    field_selectors: dict[str, FieldSelectorConfig] = {}
    json_paths: dict[str, str] = {}
    date_formats: list[str] = []
    time_formats: list[str] = []
    url_normalization: dict[str, Any] = {}
    transformations: list[TransformationRuleConfig] = []
    category_mappings: dict[str, str] = {}
    exclusion_rules: list[TransformationRuleConfig] = []
    geographic_filters: GeographicFilterConfig | None = None
    recurrence: RecurrenceRuntimeConfig | None = None
    required_fields: list[str] = ["title", "start_date", "canonical_url"]
    allow_page_url_as_canonical_fallback: bool = False
    allow_offers_url_as_event_url: bool = False
    # Optional captured request recipe for structured endpoints that need the
    # full request (nested JSON query params, public token, Referer, dynamic
    # date window, pagination). When present, preview and import render and send
    # it; when absent, behaviour is exactly as before (api_endpoint/fetch).
    request_recipe: "RequestRecipe | None" = None

    @field_validator("listing_url", "api_endpoint")
    @classmethod
    def _validate_url(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        try:
            return validate_public_url(v)
        except UnsafeURLError as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("max_detail_fetches")
    @classmethod
    def _validate_max_detail_fetches(cls, v: int) -> int:
        if v < 0:
            raise ValueError("must be zero or greater")
        return v

    @model_validator(mode="after")
    def _requires_an_endpoint(self) -> "SiteConfiguration":
        if not self.listing_url and not self.api_endpoint:
            raise ValueError("Either listing_url or api_endpoint is required")
        return self
