import re

from pydantic import BaseModel, ConfigDict, ValidationInfo, field_validator

from app.config import get_settings
from app.core.email import normalize_email

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_BCRYPT_MAX_BYTES = 72


class RegistrationCreate(BaseModel):
    """The four legitimate public self-registration fields — nothing else.

    `extra="forbid"` rejects any additional submitted field (role_id,
    is_admin, permission_id, ...) outright. This is defense-in-depth: the
    route only ever reads these four named Form(...) parameters in the first
    place, so nothing else could reach the model anyway, but a dedicated
    schema with a closed field set makes that invariant explicit and keeps it
    true even if the route is refactored later.
    """

    model_config = ConfigDict(extra="forbid")

    display_name: str
    email: str
    password: str
    password_confirm: str

    @field_validator("display_name")
    @classmethod
    def validate_display_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Display name is required")
        return v

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        v = normalize_email(v)
        if not _EMAIL_RE.fullmatch(v):
            raise ValueError("Enter a valid email address")
        return v

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Password is required")
        settings = get_settings()
        if len(v) < settings.minimum_password_length:
            raise ValueError(
                f"Password must be at least {settings.minimum_password_length} characters"
            )
        if len(v.encode("utf-8")) > _BCRYPT_MAX_BYTES:
            # No silent truncation — reject outright rather than hash only a prefix.
            raise ValueError(f"Password must be at most {_BCRYPT_MAX_BYTES} bytes")
        return v

    @field_validator("password_confirm")
    @classmethod
    def validate_password_confirm(cls, v: str, info: ValidationInfo) -> str:
        password = info.data.get("password")
        if password is not None and v != password:
            raise ValueError("Passwords do not match")
        return v
