"""Validated shared recurrence representation.

One closed schema for how a source describes a repeating event, independent of
which pattern produced it (an ICS `RRULE`, a JSON feed's explicit occurrence
array, or a single non-repeating event). It is *data*, never code: there is no
field that accepts an expression or a callable, and every list and string is
length-bounded so a hostile or broken feed cannot describe an unbounded
expansion here — the actual expansion bounds live in `RecurrenceBounds`.

Modes:

* ``parent_only`` — emit just the parent event; never generate occurrences.
  The safe default for a rule we choose not to expand.
* ``explicit_occurrences`` — the source already enumerated its occurrences
  (dates array, or detached/modified instances); use them verbatim, expand
  nothing.
* ``bounded_expand`` — expand an ``RRULE``/``RDATE`` set within the configured
  bounds, then overlay any explicit occurrences (which override generated ones)
  and exclude ``EXDATE``s.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

_MAX_RULE_LENGTH = 2_000
_MAX_LIST = 750
_MAX_ID_LENGTH = 512

RecurrenceMode = Literal["parent_only", "explicit_occurrences", "bounded_expand"]


class RecurrenceOccurrence(BaseModel):
    """One source-provided occurrence. Used both for an explicit occurrence
    array and for detached/modified instances that override a generated slot."""

    model_config = ConfigDict(extra="forbid")

    start: str = Field(min_length=1, max_length=64)  # ISO date or datetime
    end: str | None = Field(default=None, max_length=64)
    # Identity hints, in the order the expander prefers them.
    source_occurrence_id: str | None = Field(default=None, max_length=_MAX_ID_LENGTH)
    # RECURRENCE-ID: the original start of the generated slot this instance
    # replaces. Its presence marks the occurrence as a detached override.
    recurrence_id: str | None = Field(default=None, max_length=64)
    all_day: bool = False
    cancelled: bool = False
    title: str | None = Field(default=None, max_length=500)

    @property
    def is_detached(self) -> bool:
        return self.recurrence_id is not None


class RecurrenceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: RecurrenceMode = "parent_only"
    source_parent_id: str | None = Field(default=None, max_length=_MAX_ID_LENGTH)
    # Raw RRULE text (possibly multi-line: RRULE/RDATE/EXDATE). Length-capped;
    # the expander refuses sub-daily frequencies and unbounded rules.
    rrule: str | None = Field(default=None, max_length=_MAX_RULE_LENGTH)
    rdate: list[str] = Field(default_factory=list, max_length=_MAX_LIST)
    exdate: list[str] = Field(default_factory=list, max_length=_MAX_LIST)
    all_day: bool = False
    timezone: str | None = Field(default=None, max_length=64)
    occurrences: list[RecurrenceOccurrence] = Field(default_factory=list, max_length=_MAX_LIST)

    @field_validator("rdate", "exdate")
    @classmethod
    def _bound_date_strings(cls, v: list[str]) -> list[str]:
        for item in v:
            if len(item) > 64:
                raise ValueError("date value too long")
        return v


class RecurrenceBounds(BaseModel):
    """Hard limits on any expansion. Chosen conservatively; a policy may only
    tighten them. Together they cap both output size and work performed."""

    model_config = ConfigDict(extra="forbid")

    # Do not generate an occurrence starting more than this many days after the
    # reference date, nor any occurrence in the past.
    horizon_days: int = Field(default=400, ge=1, le=3_660)
    max_occurrences_per_parent: int = Field(default=200, ge=1, le=2_000)
    # A whole-run budget the orchestrator enforces across every parent.
    max_occurrences_per_run: int = Field(default=5_000, ge=1, le=50_000)
    # Reject a rule whose raw text exceeds this — a proxy for "too complex to
    # expand safely" that pairs with the sub-daily-frequency refusal.
    max_rule_length: int = Field(default=_MAX_RULE_LENGTH, ge=1, le=_MAX_RULE_LENGTH)
    # Wall-clock guard for a single parent's expansion.
    max_execution_ms: int = Field(default=2_000, ge=1, le=30_000)


def default_bounds() -> RecurrenceBounds:
    return RecurrenceBounds()
