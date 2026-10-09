"""Shared HTTP-configuration safety primitives.

Extracted into their own module so both `app.schemas.extraction` (FetchConfig)
and `app.schemas.request_recipe` (RequestRecipe) can reuse them without a
circular import.
"""

from __future__ import annotations

import re

# Headers an administrator can never set via configuration — either meaningless/
# dangerous on an outbound request we build ourselves (Host, Content-Length,
# Connection, Transfer-Encoding) or a vector for smuggling credentials/session
# state to a third party (Cookie, Authorization, Proxy-Authorization).
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

# Rejects `${VAR}`, `$VAR`, `%VAR%` environment/shell-variable references inside a
# configured string value.
_ENV_VAR_REFERENCE_RE = re.compile(
    r"\$\{[^}]+\}|\$[A-Za-z_][A-Za-z0-9_]*|%[A-Za-z_][A-Za-z0-9_]*%"
)


def reject_env_var_reference(value: str, *, field_label: str) -> str:
    if _ENV_VAR_REFERENCE_RE.search(value):
        raise ValueError(f"{field_label} must not contain an environment-variable reference")
    return value
