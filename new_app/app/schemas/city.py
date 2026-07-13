import re
from typing import Any
from zoneinfo import available_timezones

from pydantic import BaseModel, ConfigDict, field_validator

_VALID_TIMEZONES = frozenset(available_timezones())
_SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


class CityBase(BaseModel):
    name: str
    slug: str
    state_or_region: str | None = None
    country: str | None = None
    timezone: str = "UTC"
    default_latitude: float | None = None
    default_longitude: float | None = None
    boundary_config: dict[str, Any] | None = None
    is_active: bool = True

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Name is required")
        return v

    @field_validator("slug")
    @classmethod
    def validate_slug(cls, v: str) -> str:
        v = v.strip().lower()
        if not _SLUG_RE.fullmatch(v):
            raise ValueError(
                "Slug must be lowercase letters, numbers, and single hyphens only "
                "(e.g. 'bloomington-in') — no leading, trailing, or double hyphens"
            )
        return v

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, v: str) -> str:
        if v not in _VALID_TIMEZONES:
            raise ValueError(
                f"'{v}' is not a recognized IANA timezone "
                "(e.g. 'America/Indiana/Indianapolis', 'UTC')"
            )
        return v


class CityCreate(CityBase):
    pass


class CityUpdate(CityBase):
    pass


class CityRead(CityBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
