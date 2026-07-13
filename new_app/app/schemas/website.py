from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

from app.core.onboarding import ONBOARDING_STATES
from app.core.url_safety import UnsafeURLError, validate_public_url
from app.schemas.city import _VALID_TIMEZONES


class WebsiteBase(BaseModel):
    """Only the admin-editable core fields. `is_active` and `onboarding_status`
    are deliberately excluded — they're only ever changed via the transition
    service (app.services.websites), never by mass-assignment from a form."""

    name: str
    source_display_name: str | None = None
    city_id: int | None = None
    base_url: str
    event_listing_url: str | None = None
    timezone_override: str | None = None
    requires_js: bool = False
    configuration: dict[str, Any] | None = None
    schedule_config: dict[str, Any] | None = None
    proposed_pattern: dict[str, Any] | None = None
    approved_pattern: dict[str, Any] | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Name is required")
        return v

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, v: str) -> str:
        try:
            return validate_public_url(v)
        except UnsafeURLError as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("event_listing_url")
    @classmethod
    def validate_event_listing_url(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        try:
            return validate_public_url(v)
        except UnsafeURLError as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("timezone_override")
    @classmethod
    def validate_timezone_override(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        if v not in _VALID_TIMEZONES:
            raise ValueError(f"'{v}' is not a recognized IANA timezone")
        return v


class WebsiteCreate(WebsiteBase):
    pass


class WebsiteUpdate(WebsiteBase):
    pass


class WebsiteRead(WebsiteBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    is_active: bool
    onboarding_status: str

    @field_validator("onboarding_status")
    @classmethod
    def validate_onboarding_status(cls, v: str) -> str:
        if v not in ONBOARDING_STATES:
            raise ValueError(f"'{v}' is not a valid onboarding status")
        return v
