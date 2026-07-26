"""Secret references for extraction configuration.

A `SiteConfiguration` must never contain a raw credential. Instead it may name
a *reference* — `env:ALGOLIA_SEARCH_KEY` — and the value is resolved from the
environment at request time and used only to build one outbound header. The
raw value is never stored in the database, never written to an audit or
provenance record, and never logged.

Only the `env:` scheme exists today. A production secrets manager would add a
scheme here (e.g. `vault:path#field`) without changing any call site.
"""

from __future__ import annotations

import os
import re

# A reference, never a secret: an env-var name, uppercase/underscore/digits.
SECRET_REF_RE = re.compile(r"^env:[A-Z][A-Z0-9_]*$")


def is_secret_ref(value: str) -> bool:
    return bool(SECRET_REF_RE.match(value or ""))


def resolve_secret_ref(ref: str) -> str | None:
    """The referenced secret's value, or None if the reference is malformed or
    the variable is unset. The returned value is sensitive: callers must use
    it only to construct an outbound request and must never log or persist it.
    """
    if not is_secret_ref(ref):
        return None
    name = ref.split(":", 1)[1]
    value = os.environ.get(name)
    return value or None
