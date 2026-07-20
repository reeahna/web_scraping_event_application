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

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from app.core.url_safety import UnsafeURLError, validate_public_url

# Headers an administrator can never set via configuration — either they are
# meaningless/dangerous on an outbound request we build ourselves (Host,
# Content-Length, Connection, Transfer-Encoding), or they could be used to
# smuggle credentials/session state to a third-party site (Cookie,
# Authorization, Proxy-Authorization). Authorization stays blocked entirely
# until a future phase adds an explicit, secure credential system.
FORBIDDEN_HEADERS: frozenset[str] = frozenset(
    {
        "host",
        "content-length",
        "connection",
        "transfer-encoding",
        "proxy-authorization",
        "cookie",
        "authorization",
    }
)

# Rejects `${VAR}`, `$VAR`, `%VAR%` style environment/shell variable
# references appearing inside a configured string value.
_ENV_VAR_REFERENCE_RE = re.compile(r"\$\{[^}]+\}|\$[A-Za-z_][A-Za-z0-9_]*|%[A-Za-z_][A-Za-z0-9_]*%")


def _reject_env_var_reference(value: str, *, field_label: str) -> str:
    if _ENV_VAR_REFERENCE_RE.search(value):
        raise ValueError(f"{field_label} must not contain an environment-variable reference")
    return value


class FetchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: Literal["GET", "POST"] = "GET"
    headers: dict[str, str] = {}
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

    strategy: Literal["none", "query_param", "wordpress", "next_link"] = "none"
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
    geographic_filters: dict[str, Any] | None = None
    required_fields: list[str] = ["title", "start_date", "canonical_url"]
    allow_page_url_as_canonical_fallback: bool = False
    allow_offers_url_as_event_url: bool = False

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
