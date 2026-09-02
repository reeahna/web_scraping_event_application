"""Shared recurrence expander.

Turns a validated `RecurrenceSpec` into a bounded, deterministic list of
occurrences using the maintained `dateutil` rrule engine — never a hand-rolled
calendar walk. It is shared-core: every pattern that can describe recurrence
(ICS today, JSON feeds later) produces a `RecurrenceSpec` and calls this one
function, so there is no per-source recurrence code.

Safety and determinism:

* Every expansion is bounded — by a future-date horizon, a per-parent cap, a
  raw-rule-length limit, and a wall-clock execution guard. Sub-daily
  frequencies (SECONDLY/MINUTELY/HOURLY) are refused outright rather than
  expanded. The per-run cap is enforced by the caller across parents.
* Occurrence identity is deterministic and stable across runs, preferring, in
  order: a source occurrence id; the source parent id plus RECURRENCE-ID; the
  parent fingerprint plus the normalized start; and finally a bounded hash of
  that key. This is what lets a re-run recognise the same occurrence instead of
  duplicating it.
* Times are treated as wall-clock: an event at 19:00 local recurs at 19:00
  local every time, which is the DST-correct behaviour for a local listing (the
  engine performs no absolute-instant timezone math — see normalize.py).
* Explicit/detached occurrences OVERRIDE the generated slot they replace, and a
  cancelled occurrence is flagged, never dropped — the persistence layer hides
  or deactivates it rather than deleting, so history is preserved.
"""

from __future__ import annotations

import dataclasses
import hashlib
import re
import time as _time
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any

from dateutil.rrule import rrulestr

from app.extraction.transform import parse_date_value
from app.schemas.recurrence import RecurrenceBounds, RecurrenceOccurrence, RecurrenceSpec

if TYPE_CHECKING:
    from app.extraction.types import EventCandidate
    from app.schemas.extraction import SiteConfiguration

_SUBDAILY = ("FREQ=SECONDLY", "FREQ=MINUTELY", "FREQ=HOURLY")
_MAX_IDENTITY_LENGTH = 200


@dataclass(frozen=True)
class ExpandedOccurrence:
    identity: str
    start_date: date
    start_time: time | None
    end_date: date | None
    end_time: time | None
    all_day: bool
    cancelled: bool
    detached: bool
    source_occurrence_id: str | None
    title: str | None


@dataclass
class RecurrenceExpansion:
    occurrences: list[ExpandedOccurrence] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    truncated: bool = False
    mode: str = "parent_only"


def parse_iso_temporal(value: str | None) -> datetime | date | None:
    """Parse an ISO date or date-time. All-day values are dates; everything
    else a (possibly naive) datetime. Never raises."""
    if not value:
        return None
    text = value.strip()
    try:
        if "T" in text or " " in text and len(text) > 10:
            return datetime.fromisoformat(text)
        if len(text) == 10:
            return date.fromisoformat(text)
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _split(temporal: datetime | date | None) -> tuple[date | None, time | None]:
    if temporal is None:
        return None, None
    if isinstance(temporal, datetime):
        return temporal.date(), temporal.time().replace(tzinfo=None)
    return temporal, None


def occurrence_identity(
    *,
    parent_fingerprint: str,
    source_parent_id: str | None,
    source_occurrence_id: str | None,
    recurrence_id: str | None,
    normalized_start: str,
) -> str:
    if source_occurrence_id:
        key = f"srcocc|{source_occurrence_id}"
    elif source_parent_id and recurrence_id:
        key = f"parentrec|{source_parent_id}|{recurrence_id}"
    else:
        key = f"fp|{parent_fingerprint}|{normalized_start}"
    if len(key) > _MAX_IDENTITY_LENGTH:
        # Stable bounded fallback: the key is deterministic, so its hash is too.
        return "h|" + hashlib.sha256(key.encode("utf-8")).hexdigest()
    return key


def _occurrence_from_source(
    occ: RecurrenceOccurrence,
    *,
    parent_fingerprint: str,
    source_parent_id: str | None,
) -> ExpandedOccurrence | None:
    start = parse_iso_temporal(occ.start)
    if start is None:
        return None
    start_date, start_time = _split(start)
    end_date, end_time = _split(parse_iso_temporal(occ.end))
    identity = occurrence_identity(
        parent_fingerprint=parent_fingerprint,
        source_parent_id=source_parent_id,
        source_occurrence_id=occ.source_occurrence_id,
        recurrence_id=occ.recurrence_id,
        normalized_start=start_date.isoformat(),
    )
    return ExpandedOccurrence(
        identity=identity,
        start_date=start_date,
        start_time=None if occ.all_day else start_time,
        end_date=end_date,
        end_time=None if occ.all_day else end_time,
        all_day=occ.all_day,
        cancelled=occ.cancelled,
        detached=occ.is_detached,
        source_occurrence_id=occ.source_occurrence_id,
        title=occ.title,
    )


def expand_recurrence(
    spec: RecurrenceSpec,
    *,
    parent_start: datetime | date,
    parent_end: datetime | date | None = None,
    parent_fingerprint: str,
    reference_date: date,
    bounds: RecurrenceBounds | None = None,
) -> RecurrenceExpansion:
    """Deterministic given identical inputs. `reference_date` (today) is passed
    in rather than read from the clock so expansion is reproducible and
    testable."""
    bounds = bounds or RecurrenceBounds()
    result = RecurrenceExpansion(mode=spec.mode)

    if spec.mode == "parent_only":
        result.occurrences = [
            _parent_occurrence(spec, parent_start, parent_end, parent_fingerprint)
        ]
        return result

    if spec.mode == "explicit_occurrences":
        _apply_explicit(spec, result, parent_fingerprint)
        return result

    # bounded_expand
    if spec.rrule and len(spec.rrule) > bounds.max_rule_length:
        result.warnings.append("recurrence_rule_too_long")
        result.occurrences = [
            _parent_occurrence(spec, parent_start, parent_end, parent_fingerprint)
        ]
        return result

    generated = _expand_rule(
        spec, parent_start, parent_end, parent_fingerprint, reference_date, bounds, result
    )
    # Overlay explicit/detached occurrences: they replace a generated slot with
    # the same recurrence-id/start, and cancelled ones flag (never delete) it.
    _overlay_explicit(spec, generated, parent_fingerprint)
    result.occurrences = list(generated.values())
    result.occurrences.sort(key=lambda o: (o.start_date, o.start_time or time.min))
    return result


def _parent_occurrence(spec, parent_start, parent_end, parent_fingerprint) -> ExpandedOccurrence:
    start_date, start_time = _split(parent_start)
    end_date, end_time = _split(parent_end)
    identity = occurrence_identity(
        parent_fingerprint=parent_fingerprint,
        source_parent_id=spec.source_parent_id,
        source_occurrence_id=None,
        recurrence_id=None,
        normalized_start=(start_date or reference_zero()).isoformat(),
    )
    return ExpandedOccurrence(
        identity=identity,
        start_date=start_date or reference_zero(),
        start_time=None if spec.all_day else start_time,
        end_date=end_date,
        end_time=None if spec.all_day else end_time,
        all_day=spec.all_day,
        cancelled=False,
        detached=False,
        source_occurrence_id=None,
        title=None,
    )


def reference_zero() -> date:
    # A parent with no parseable start still yields one identity-bearing row
    # rather than vanishing; the validator will reject it downstream for the
    # missing date. Kept deterministic and obvious.
    return date(1970, 1, 1)


def _apply_explicit(
    spec: RecurrenceSpec, result: RecurrenceExpansion, parent_fingerprint: str
) -> None:
    for occ in spec.occurrences:
        built = _occurrence_from_source(
            occ, parent_fingerprint=parent_fingerprint, source_parent_id=spec.source_parent_id
        )
        if built is not None:
            result.occurrences.append(built)
        else:
            result.warnings.append("recurrence_occurrence_unparseable")


def _expand_rule(
    spec, parent_start, parent_end, parent_fingerprint, reference_date, bounds, result
):
    generated: dict[str, ExpandedOccurrence] = {}
    if not spec.rrule and not spec.rdate:
        # Nothing to expand — treat as the parent alone.
        parent = _parent_occurrence(spec, parent_start, parent_end, parent_fingerprint)
        generated[parent.identity] = parent
        return generated

    dtstart = parent_start if isinstance(parent_start, datetime) else datetime.combine(
        parent_start, time.min
    )
    ref_start = datetime.combine(reference_date, time.min)
    horizon_end = datetime.combine(reference_date + timedelta(days=bounds.horizon_days), time.max)

    # An RRULE is optional: a spec may enumerate its occurrences purely through
    # RDATEs (a source that lists explicit dates but states no rule). Only parse
    # a rule when one is present — feeding an empty body to rrulestr would raise
    # and wrongly collapse an rdate-only series back to a single parent.
    instants: list[datetime] = []
    if spec.rrule:
        upper = spec.rrule.upper()
        if any(marker in upper for marker in _SUBDAILY):
            result.warnings.append("recurrence_subdaily_refused")
            parent = _parent_occurrence(spec, parent_start, parent_end, parent_fingerprint)
            generated[parent.identity] = parent
            return generated
        try:
            rule = rrulestr(_rrule_body(spec.rrule), dtstart=dtstart)
        except (ValueError, TypeError):
            result.warnings.append("recurrence_rule_unparseable")
            parent = _parent_occurrence(spec, parent_start, parent_end, parent_fingerprint)
            generated[parent.identity] = parent
            return generated

        started = _time.monotonic()
        for instant in rule:
            if instant < ref_start:
                continue
            if instant > horizon_end:
                break
            instants.append(instant)
            if len(instants) >= bounds.max_occurrences_per_parent:
                result.truncated = True
                result.warnings.append("recurrence_truncated_per_parent")
                break
            if (_time.monotonic() - started) * 1000 > bounds.max_execution_ms:
                result.truncated = True
                result.warnings.append("recurrence_execution_budget_exceeded")
                break

    for d in spec.rdate:
        extra = parse_iso_temporal(d)
        if isinstance(extra, date) and not isinstance(extra, datetime):
            extra = datetime.combine(extra, time.min)
        if isinstance(extra, datetime) and ref_start <= extra <= horizon_end:
            instants.append(extra)

    excluded = {parse_iso_temporal(d) for d in spec.exdate}
    excluded_dates = {e.date() if isinstance(e, datetime) else e for e in excluded if e}

    duration = _duration(parent_start, parent_end)
    all_day = spec.all_day or not isinstance(parent_start, datetime)
    for instant in instants:
        if instant.date() in excluded_dates:
            continue
        occ = _generated_occurrence(
            instant, duration, all_day, spec, parent_fingerprint
        )
        generated[occ.identity] = occ
    return generated


def _generated_occurrence(
    instant, duration, all_day, spec, parent_fingerprint
) -> ExpandedOccurrence:
    start_date = instant.date()
    start_time = None if all_day else instant.time().replace(tzinfo=None)
    end_date = end_time = None
    if duration is not None:
        end_dt = instant + duration
        end_date = end_dt.date()
        end_time = None if all_day else end_dt.time().replace(tzinfo=None)
    identity = occurrence_identity(
        parent_fingerprint=parent_fingerprint,
        source_parent_id=spec.source_parent_id,
        source_occurrence_id=None,
        recurrence_id=None,
        normalized_start=start_date.isoformat(),
    )
    return ExpandedOccurrence(
        identity=identity,
        start_date=start_date,
        start_time=start_time,
        end_date=end_date,
        end_time=end_time,
        all_day=all_day,
        cancelled=False,
        detached=False,
        source_occurrence_id=None,
        title=None,
    )


def _overlay_explicit(spec, generated: dict, parent_fingerprint: str) -> None:
    for occ in spec.occurrences:
        built = _occurrence_from_source(
            occ, parent_fingerprint=parent_fingerprint, source_parent_id=spec.source_parent_id
        )
        if built is None:
            continue
        # A detached instance replaces the generated slot it was derived from.
        # That slot is identified by RECURRENCE-ID (the instance's *original*
        # start), not by the possibly-moved new start; only when there is no
        # usable RECURRENCE-ID do we fall back to matching on the new start.
        # Cancellation flags the matched slot rather than deleting it.
        match_date, match_time = built.start_date, built.start_time
        if occ.recurrence_id:
            original_date, original_time = _split(parse_iso_temporal(occ.recurrence_id))
            if original_date is not None:
                match_date, match_time = original_date, original_time
        target_key = None
        for key, gen in generated.items():
            if gen.start_date == match_date and gen.start_time == match_time:
                target_key = key
                break
        if target_key is not None:
            generated[target_key] = built
        else:
            generated[built.identity] = built


def _rrule_body(rrule: str | None) -> str:
    text = (rrule or "").strip()
    if text.upper().startswith("RRULE:"):
        return text[len("RRULE:"):]
    return text


def _duration(parent_start, parent_end) -> timedelta | None:
    if parent_end is None:
        return None
    start = parent_start if isinstance(parent_start, datetime) else datetime.combine(
        parent_start, time.min
    )
    end = parent_end if isinstance(parent_end, datetime) else datetime.combine(parent_end, time.min)
    delta = end - start
    return delta if delta.total_seconds() >= 0 else None


# --- pipeline integration ----------------------------------------------------


def _has_payload(rec: dict, mode: str) -> bool:
    if mode == "explicit_occurrences":
        return bool(rec.get("occurrences"))
    # bounded_expand needs at least an rrule or explicit rdates.
    return bool(rec.get("rrule") or rec.get("rdate") or rec.get("occurrences"))


def _occurrence_rdates(text: Any, formats: list[str]) -> list[str]:
    """Parse a delimited list of dates carried on a candidate's raw
    ``occurrence_dates`` (a detail page's explicit "Dates:" list, e.g.
    "9/4/2026, 10/2/2026, 11/6/2026") into ISO date strings usable as RDATEs.

    Tokens are parsed with the site's configured date formats first, then ISO;
    an unparseable token is skipped rather than failing the whole event. This is
    a generic convention — any pattern that fills ``occurrence_dates`` benefits;
    nothing here is source-specific."""
    if not isinstance(text, str) or not text.strip():
        return []
    iso: list[str] = []
    for token in re.split(r"[,;\n]+", text):
        token = token.strip()
        if not token:
            continue
        parsed = parse_date_value(token, formats)
        if parsed is not None:
            iso.append(parsed.isoformat())
    # De-duplicate while preserving order — a page may repeat a date.
    return list(dict.fromkeys(iso))


def _spec_from_raw(rec: dict, mode: str, all_day: bool) -> RecurrenceSpec:
    def _as_list(value) -> list[str]:
        if value is None:
            return []
        if isinstance(value, list):
            return [str(v) for v in value]
        return [str(value)]

    occurrences: list[RecurrenceOccurrence] = []
    for item in rec.get("occurrences") or []:
        if isinstance(item, dict):
            try:
                occurrences.append(RecurrenceOccurrence.model_validate(item))
            except (ValueError, TypeError):
                continue
    return RecurrenceSpec(
        mode=mode,  # type: ignore[arg-type]
        source_parent_id=rec.get("source_parent_id") or rec.get("recurrence_id"),
        rrule=rec.get("rrule"),
        rdate=_as_list(rec.get("rdate")),
        exdate=_as_list(rec.get("exdate")),
        all_day=all_day,
        occurrences=occurrences,
    )


def _candidate_temporal(cand: EventCandidate) -> datetime | date | None:
    if cand.start_date is None:
        return None
    if cand.start_time is None:
        return cand.start_date
    return datetime.combine(cand.start_date, cand.start_time)


def _candidate_end_temporal(cand: EventCandidate) -> datetime | date | None:
    if cand.end_date is None:
        return None
    if cand.end_time is None:
        return cand.end_date
    return datetime.combine(cand.end_date, cand.end_time)


def expand_candidates(
    candidates: list[EventCandidate],
    config: SiteConfiguration,
    *,
    reference_date: date,
) -> tuple[list[EventCandidate], list[str]]:
    """Expand any recurrence-bearing candidate into per-occurrence candidates,
    honouring the config mode and a whole-run occurrence budget. A candidate
    without recurrence data, or a parent_only config, passes through unchanged.
    Pure and deterministic given `reference_date`."""
    rconfig = config.recurrence
    if rconfig is None or rconfig.mode == "parent_only":
        return candidates, []

    warnings: list[str] = []
    out: list[EventCandidate] = []
    produced = 0
    budget = rconfig.bounds.max_occurrences_per_run
    for cand in candidates:
        rec = cand.raw.get("recurrence")
        # A detail page may enumerate an event's dates explicitly ("Dates:
        # 9/4/2026, 10/2/2026, ...") when the listing states no rule. Fold that
        # list into the spec as RDATEs, unless a real recurrence rule was
        # already recognised (a rule + these dates would double-count).
        if not (isinstance(rec, dict) and rec.get("rrule")):
            rdates = _occurrence_rdates(cand.raw.get("occurrence_dates"), config.date_formats)
            if rdates:
                rec = {**(rec if isinstance(rec, dict) else {}), "rdate": rdates}
        parent_start = _candidate_temporal(cand)
        if not isinstance(rec, dict) or not _has_payload(rec, rconfig.mode) or parent_start is None:
            out.append(cand)
            continue
        all_day = bool(rec.get("all_day")) or cand.start_time is None
        spec = _spec_from_raw(rec, rconfig.mode, all_day)
        parent_fp = cand.external_source_id or cand.raw_record_hash
        expansion = expand_recurrence(
            spec,
            parent_start=parent_start,
            parent_end=_candidate_end_temporal(cand),
            parent_fingerprint=parent_fp,
            reference_date=reference_date,
            bounds=rconfig.bounds,
        )
        warnings.extend(expansion.warnings)
        if not expansion.occurrences:
            out.append(cand)
            continue
        for occ in expansion.occurrences:
            if produced >= budget:
                warnings.append("recurrence_run_budget_exceeded")
                break
            out.append(_child_candidate(cand, occ, spec, rconfig.mode))
            produced += 1
        else:
            continue
        break
    return out, warnings


def _child_candidate(cand, occ: ExpandedOccurrence, spec, mode: str) -> EventCandidate:
    history = (*cand.transformation_history, f"recurrence:{mode}")
    warnings = cand.warnings
    if occ.cancelled:
        warnings = (*warnings, "recurrence_occurrence_cancelled")
    return dataclasses.replace(
        cand,
        title=occ.title or cand.title,
        start_date=occ.start_date,
        start_time=occ.start_time,
        end_date=occ.end_date if occ.end_date is not None else None,
        end_time=occ.end_time,
        # Each occurrence dedups on its own stable identity, never on the shared
        # parent UID (which would collapse the whole series to one row).
        external_source_id=occ.identity,
        occurrence_id=occ.identity,
        recurrence_parent_id=(
            spec.source_parent_id or cand.external_source_id or cand.raw_record_hash
        ),
        is_cancelled=occ.cancelled,
        transformation_history=history,
        warnings=warnings,
    )
