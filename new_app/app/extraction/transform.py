"""Transformation rules: a restricted, validated set of value transforms.

`TransformationRuleConfig` (app.schemas.extraction) *is* the TransformationRule
abstraction — a closed `Literal` kind + a plain-data params dict. There is no
way to store executable code here: `apply_transformations` dispatches on
`kind` through a fixed dict of pure functions, never `eval`/`exec`, and never
a callable loaded from configuration.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from datetime import date, datetime, time
from typing import Any
from urllib.parse import urljoin

from app.core.safe_regex import validate_safe_regex
from app.schemas.extraction import TransformationRuleConfig

_WHITESPACE_RE = re.compile(r"\s+")
_TAG_RE = re.compile(r"<[^>]+>")
_MAX_INPUT_LENGTH = 10_000


class TransformationError(ValueError):
    pass


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    if len(text) > _MAX_INPUT_LENGTH:
        text = text[:_MAX_INPUT_LENGTH]
    return text


def _trim(value: Any, params: dict[str, Any]) -> Any:
    return _as_text(value).strip() if value is not None else value


def _collapse_whitespace(value: Any, params: dict[str, Any]) -> Any:
    return _WHITESPACE_RE.sub(" ", _as_text(value)).strip() if value is not None else value


def _strip_html(value: Any, params: dict[str, Any]) -> Any:
    return _TAG_RE.sub("", _as_text(value)) if value is not None else value


def _unicode_normalize(value: Any, params: dict[str, Any]) -> Any:
    form = params.get("form", "NFKC")
    if form not in {"NFC", "NFKC", "NFD", "NFKD"}:
        raise TransformationError(f"Unsupported unicode normalization form: {form}")
    return unicodedata.normalize(form, _as_text(value)) if value is not None else value


def _prepend(value: Any, params: dict[str, Any]) -> Any:
    text = str(params.get("text", ""))
    return f"{text}{_as_text(value)}" if value is not None else value


def _append(value: Any, params: dict[str, Any]) -> Any:
    text = str(params.get("text", ""))
    return f"{_as_text(value)}{text}" if value is not None else value


def parse_date_value(value: Any, formats: list[str] | None = None) -> date | None:
    """Core deterministic date parser: tries each of `formats` (site-configured
    strptime formats, tried in the given order — first match wins, never a
    per-site special case) before falling back to ISO 8601. Used both as the
    `parse_date` transformation and directly by EventNormalizer for the
    required start/end date parsing step."""
    if value is None:
        return None
    if isinstance(value, date):
        return value
    text = _as_text(value).strip()
    if not text:
        return None
    for fmt in formats or []:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def parse_time_value(value: Any, formats: list[str] | None = None) -> time | None:
    """Core deterministic time parser — see parse_date_value. Accepts
    ISO 8601 times including a timezone offset (e.g. "19:00:00-05:00"),
    which Python's time.fromisoformat supports directly."""
    if value is None:
        return None
    if isinstance(value, time):
        return value
    text = _as_text(value).strip()
    if not text:
        return None
    for fmt in formats or []:
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    try:
        return time.fromisoformat(text)
    except ValueError:
        return None


def _parse_date(value: Any, params: dict[str, Any]) -> date | None:
    return parse_date_value(value, params.get("formats"))


def _parse_time(value: Any, params: dict[str, Any]) -> time | None:
    return parse_time_value(value, params.get("formats"))


def _relative_to_absolute_url(value: Any, params: dict[str, Any]) -> Any:
    if value is None:
        return None
    base_url = params.get("base_url")
    if not base_url:
        raise TransformationError("relative_to_absolute_url requires a base_url param")
    return urljoin(base_url, _as_text(value))


def _regex_extract_group(value: Any, params: dict[str, Any]) -> Any:
    if value is None:
        return None
    pattern = params.get("pattern")
    if not pattern:
        raise TransformationError("regex_extract_group requires a pattern param")
    validate_safe_regex(pattern)
    group = params.get("group", 1)
    match = re.search(pattern, _as_text(value))
    if not match:
        return None
    try:
        return match.group(group)
    except IndexError:
        return None


def _literal_replace(value: Any, params: dict[str, Any]) -> Any:
    if value is None:
        return None
    find = str(params.get("find", ""))
    replace = str(params.get("replace", ""))
    if not find:
        return value
    return _as_text(value).replace(find, replace)


def _exact_value_map(value: Any, params: dict[str, Any]) -> Any:
    if value is None:
        return None
    mapping: dict[str, Any] = params.get("mapping") or {}
    return mapping.get(_as_text(value), value)


def _lower(value: Any, params: dict[str, Any]) -> Any:
    return _as_text(value).lower() if value is not None else value


def _upper(value: Any, params: dict[str, Any]) -> Any:
    return _as_text(value).upper() if value is not None else value


_TRANSFORMS: dict[str, Callable[[Any, dict[str, Any]], Any]] = {
    "trim": _trim,
    "collapse_whitespace": _collapse_whitespace,
    "strip_html": _strip_html,
    "unicode_normalize": _unicode_normalize,
    "prepend": _prepend,
    "append": _append,
    "parse_date": _parse_date,
    "parse_time": _parse_time,
    "relative_to_absolute_url": _relative_to_absolute_url,
    "regex_extract_group": _regex_extract_group,
    "literal_replace": _literal_replace,
    "exact_value_map": _exact_value_map,
    "lower": _lower,
    "upper": _upper,
}


def apply_transformations(
    value: Any, rules: list[TransformationRuleConfig]
) -> tuple[Any, list[str]]:
    """Applies `rules` in order; returns (final_value, history) where history
    is a list of "kind" strings recording which transformations actually ran
    — used to populate EventCandidate.transformation_history for provenance."""
    history: list[str] = []
    current = value
    for rule in rules:
        transform = _TRANSFORMS[rule.kind]
        current = transform(current, rule.params)
        history.append(rule.kind)
    return current, history
