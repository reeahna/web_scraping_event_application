"""Presentation view-model for preview-quality snapshots.

Preview quality is persisted as a plain JSON dict (in `Website.proposed_pattern`
and `OnboardingJob.quality`), so its keys are whatever `PreviewQualityResult`
emitted *at the time the snapshot was written*. Metrics added later (the Phase
8G date-range and geographic metrics) are simply absent from older snapshots.

`quality_view` normalizes any such mapping (or a live `PreviewQualityResult`
object) into a stable view-model with **explicit nullable** optional fields, so
a template can tell three things apart:

* the metric was never evaluated / predates the snapshot  -> value is ``None``
* the metric exists and is genuinely zero                 -> value is ``0.0``/``0``
* the metric exists with a real value                     -> that value

It never turns a missing metric into a 0% that would falsely imply a failed
evaluation. It is presentation only — it computes nothing and changes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass


def _get(source, key):
    """Read `key` from a mapping or an object (or a Jinja Undefined), returning
    None when absent — the single 'absent' sentinel the view-model relies on."""
    if source is None:
        return None
    if isinstance(source, dict):
        return source.get(key)
    return getattr(source, key, None)


def _int_or_zero(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


@dataclass(frozen=True)
class QualityView:
    # Core metrics — present in every snapshot ever written. Counts default to
    # 0; rates stay nullable so `pct` can render "—" for a truly partial one.
    candidates_found: int
    valid_count: int
    rejected_count: int
    valid_percentage: float | None
    rejected_percentage: float | None
    required_field_coverage: dict
    date_parse_success_rate: float | None
    url_validity_rate: float | None
    duplicate_rate: float | None
    warning_count: int
    pagination_truncated: bool
    detail_fetch_used: bool
    pages_fetched: int

    # Optional metrics (Phase 8G+). Nullable: None means "not present in this
    # snapshot", distinct from an evaluated zero.
    range_candidate_count: int | None
    range_parse_success_rate: float | None
    end_date_parse_success_rate: float | None
    ambiguous_range_rejection_count: int | None
    geographic_considered: int | None
    geographic_included: int | None
    geographic_excluded: int | None
    geographic_missing: int | None
    geographic_inclusion_rate: float | None

    @property
    def range_applicable(self) -> bool:
        """True when this snapshot actually evaluated any date-range activity
        (multi-day events found, or ambiguous ranges rejected). When False the
        UI shows 'not applicable' rather than a misleading 0%/100%."""
        return (self.range_candidate_count or 0) > 0 or (
            self.ambiguous_range_rejection_count or 0
        ) > 0

    @property
    def range_metrics_recorded(self) -> bool:
        """True when a range parse-success rate is present. False (with
        `range_applicable` True) means 'ranges seen but the metric predates
        this snapshot' -> render 'not recorded', not 0%."""
        return self.range_parse_success_rate is not None

    @property
    def geographic_configured(self) -> bool:
        return (self.geographic_considered or 0) > 0


def quality_view(source) -> QualityView | None:
    """Normalize a persisted quality mapping / `PreviewQualityResult` / None /
    Jinja Undefined into a QualityView, or None when there is no quality to
    show. `not source` is True for None, an empty dict, and a Jinja Undefined."""
    if not source:
        return None
    return QualityView(
        candidates_found=_int_or_zero(_get(source, "candidates_found")),
        valid_count=_int_or_zero(_get(source, "valid_count")),
        rejected_count=_int_or_zero(_get(source, "rejected_count")),
        valid_percentage=_get(source, "valid_percentage"),
        rejected_percentage=_get(source, "rejected_percentage"),
        required_field_coverage=_get(source, "required_field_coverage") or {},
        date_parse_success_rate=_get(source, "date_parse_success_rate"),
        url_validity_rate=_get(source, "url_validity_rate"),
        duplicate_rate=_get(source, "duplicate_rate"),
        warning_count=_int_or_zero(_get(source, "warning_count")),
        pagination_truncated=bool(_get(source, "pagination_truncated")),
        detail_fetch_used=bool(_get(source, "detail_fetch_used")),
        pages_fetched=_int_or_zero(_get(source, "pages_fetched")),
        # Optional (kept nullable — do NOT coalesce to 0).
        range_candidate_count=_get(source, "range_count"),
        range_parse_success_rate=_get(source, "range_parse_success_rate"),
        end_date_parse_success_rate=_get(source, "end_date_success_rate"),
        ambiguous_range_rejection_count=_get(source, "ambiguous_range_rejections"),
        geographic_considered=_get(source, "geographic_considered"),
        geographic_included=_get(source, "geographic_included"),
        geographic_excluded=_get(source, "geographic_excluded"),
        geographic_missing=_get(source, "geographic_missing"),
        geographic_inclusion_rate=_get(source, "geographic_inclusion_rate"),
    )


def format_percent(value) -> str:
    """Render a 0..1 rate as a whole percent. A present zero -> '0%'; an absent
    value (None / a Jinja Undefined / non-numeric) -> '—', so the display
    distinguishes 'not evaluated' from an evaluated zero. Must never raise —
    a Jinja `Undefined` raises `UndefinedError` (not TypeError) from `float()`,
    so this catches broadly on purpose."""
    if value is None:
        return "—"
    try:
        return f"{float(value) * 100:.0f}%"
    except Exception:  # noqa: BLE001 - a presentation filter must never raise
        return "—"
