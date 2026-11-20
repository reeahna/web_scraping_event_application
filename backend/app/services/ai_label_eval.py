"""Side-by-side evaluation of AI labeling setups on one fixed sample of events.

An administrator starts an evaluation from the AI labeling page. It freezes a
sample of about 100 events (spread over concerts and performances, workshops
and exhibitions, student groups, lectures and seminars, internal meetings,
online events, events with little to go on, events the AI already hid, and
events whose category an administrator set by hand) and then labels that same
sample with each setup:

* ``baseline``: the setup production uses now.
* ``lean``: the same model with shorter descriptions, the split prompt layout
  (cacheable instructions) and larger batches.
* the candidate model (``openai/gpt-oss-20b`` by default) with each of the two.

The scheduler process runs it one request per tick, in place of the labeling
drain, and keeps a separate cooldown per model so the candidate's own free
quota is used while the main model waits. Nothing it produces is written to an
event: the report only compares the setups' answers with the baseline (and
with administrator overrides, which are the closest thing to ground truth),
category and keep decision separately, and lists every disagreement.
"""

from __future__ import annotations

import logging
import random
import re
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.ai_label_evaluation import AiLabelEvaluation
from app.models.event import Event
from app.models.event_category import EventCategory
from app.services.ai_categorizer import (
    PROMPT_VERSION,
    EventToClassify,
    GeminiBadOutput,
    GeminiError,
    GeminiQuotaExceeded,
    GroqCategorizer,
    sanitize_error,
)

logger = logging.getLogger(__name__)

RUNNING = "running"
DONE = "done"
CANCELLED = "cancelled"
FAILED = "failed"

DEFAULT_SAMPLE_SIZE = 100
PER_BUCKET = 12
MAX_OVERRIDES = 15
# A chunk that keeps failing is given up after this many tries; its events
# then count as unanswered for that setup.
MAX_CHUNK_TRIES = 3
# How far back a past event may be and still be sampled.
RECENT_DAYS = 45

BUCKETS: dict[str, re.Pattern[str]] = {
    "performances": re.compile(
        r"\b(concert|band|orchestra|symphony|recital|choir|chorale|jazz|opera|theat(?:er|re)"
        r"|musical|comedy|improv|performance|performs?|dance)\b",
        re.I,
    ),
    "workshops_exhibitions": re.compile(
        r"\b(workshop|exhibit(?:ion)?|gallery|class|craft|studio|demonstration|museum)\b", re.I
    ),
    "student_orgs": re.compile(
        r"\b(club|students?|organization|association|society|fraternity|sorority|chapter"
        r"|student union|rso)\b",
        re.I,
    ),
    "lectures_seminars": re.compile(
        r"\b(lecture|seminar|colloquium|talk|symposium|speaker|panel|keynote|defen[cs]e"
        r"|dissertation|workshop series)\b",
        re.I,
    ),
    "internal_meetings": re.compile(
        r"\b(meeting|committee|board|council|senate|staff|faculty|working group"
        r"|office hours|retreat|training)\b",
        re.I,
    ),
    "online": re.compile(
        r"\b(online|virtual|zoom|webinar|livestream|live stream|teams meeting|remote)\b", re.I
    ),
}


def default_configs() -> list[dict[str, Any]]:
    settings = get_settings()
    main = settings.groq_model
    candidate = settings.groq_eval_candidate_model
    current = {
        "description_chars": settings.ai_description_chars,
        "layout": settings.groq_prompt_layout,
        "batch_size": settings.groq_batch_size,
    }
    lean = {"description_chars": 300, "layout": "split", "batch_size": 40}
    configs = [
        {"key": "baseline", "model": main, **current},
        {"key": "lean", "model": main, **lean},
    ]
    if candidate and candidate != main:
        configs += [
            {"key": "candidate", "model": candidate, **current},
            {"key": "candidate_lean", "model": candidate, **lean},
        ]
    return configs


# --- Sampling -------------------------------------------------------------------


def _buckets_for(event: Event) -> list[str]:
    text = " ".join(
        part for part in (event.title, event.source_category, (event.description or "")[:400])
        if part
    )
    found = [name for name, pattern in BUCKETS.items() if pattern.search(text)]
    if len((event.description or "").strip()) < 80 or len(event.title.split()) <= 3:
        found.append("little_information")
    if not event.is_active and event.category_source == "ai":
        found.append("hidden_by_ai")
    if event.category_override_id is not None:
        found.append("admin_override")
    return found


def select_sample(
    db: Session, *, size: int = DEFAULT_SAMPLE_SIZE, seed: int = 2026, today: date | None = None
) -> list[dict[str, Any]]:
    """About `size` recent or upcoming events, at least PER_BUCKET from each
    bucket that has that many, topped up at random. Deterministic for a seed."""
    today = today or date.today()
    since = today - timedelta(days=RECENT_DAYS)
    pool = (
        db.query(Event)
        .filter(
            Event.archived_at.is_(None),
            Event.is_recurrence_parent.is_(False),
            Event.is_cancelled.is_not(True),
            Event.duplicate_status != "confirmed_duplicate",
            or_(Event.start_date >= since, Event.end_date >= since),
            # Visible events, plus hidden ones only when the AI hid them.
            or_(Event.is_active.is_(True), Event.category_source == "ai"),
        )
        .order_by(Event.id.desc())
        .limit(6000)
        .all()
    )
    rng = random.Random(seed)
    by_bucket: dict[str, list[Event]] = {}
    for event in pool:
        for bucket in _buckets_for(event):
            by_bucket.setdefault(bucket, []).append(event)

    chosen: dict[int, Event] = {}
    quotas = {name: PER_BUCKET for name in [*BUCKETS, "little_information", "hidden_by_ai"]}
    quotas["admin_override"] = MAX_OVERRIDES
    for bucket, quota in quotas.items():
        members = by_bucket.get(bucket, [])
        # An event already chosen for an earlier bucket counts toward this one
        # too, so only the shortfall is drawn from the bucket's other events.
        already = sum(1 for e in members if e.id in chosen)
        fresh = [e for e in members if e.id not in chosen]
        rng.shuffle(fresh)
        for event in fresh[: max(0, quota - already)]:
            chosen[event.id] = event
    rest = [e for e in pool if e.id not in chosen]
    rng.shuffle(rest)
    for event in rest:
        if len(chosen) >= size:
            break
        chosen[event.id] = event

    category_slugs = {c.id: c.slug for c in db.query(EventCategory).all()}
    sample = []
    for event in sorted(chosen.values(), key=lambda e: e.id):
        stored = (
            category_slugs.get(event.category_id)
            if event.category_source == "ai" and event.ai_prompt_version == PROMPT_VERSION
            else None
        )
        sample.append({
            "id": event.id,
            "title": event.title,
            "description": event.description,
            "source_category": event.source_category,
            "venue": event.venue,
            "buckets": _buckets_for(event),
            "stored_category": stored,
            "stored_keep": event.is_active if stored else None,
            "override_category": category_slugs.get(event.category_override_id),
        })
    return sample


# --- Lifecycle ------------------------------------------------------------------


def running_evaluation(db: Session) -> AiLabelEvaluation | None:
    return (
        db.query(AiLabelEvaluation)
        .filter(AiLabelEvaluation.status == RUNNING)
        .order_by(AiLabelEvaluation.id.asc())
        .first()
    )


def start_evaluation(
    db: Session, *, user_id: int | None, size: int = DEFAULT_SAMPLE_SIZE
) -> AiLabelEvaluation:
    """Freeze a sample and queue it for the scheduler. Does not commit.
    Raises ValueError when one is already running or Groq is not configured."""
    if not get_settings().groq_api_key:
        raise ValueError("A Groq API key is needed to run an evaluation.")
    if running_evaluation(db) is not None:
        raise ValueError("An evaluation is already running.")
    configs = default_configs()
    evaluation = AiLabelEvaluation(
        created_by_user_id=user_id,
        status=RUNNING,
        configs=configs,
        sample=select_sample(db, size=size),
        progress={c["key"]: _empty_progress() for c in configs},
    )
    db.add(evaluation)
    return evaluation


def cancel_evaluation(evaluation: AiLabelEvaluation) -> None:
    evaluation.status = CANCELLED
    evaluation.finished_at = datetime.now(UTC)


def _empty_progress() -> dict[str, Any]:
    return {
        "labels": {},
        "done": [],
        "tries": {},
        "requests": 0,
        "tokens": {"prompt": 0, "completion": 0, "total": 0, "cached": 0, "reasoning": 0},
        "rate_limited": 0,
        "errors": [],
    }


def _chunks(evaluation: AiLabelEvaluation, config: dict[str, Any]) -> list[list[dict]]:
    size = max(1, int(config["batch_size"]))
    sample = evaluation.sample
    return [sample[i:i + size] for i in range(0, len(sample), size)]


def _cooling(progress: dict[str, Any], model: str, now: datetime) -> bool:
    until = (progress.get("_cooldowns") or {}).get(model)
    return bool(until) and datetime.fromisoformat(until) > now


def _next_step(
    evaluation: AiLabelEvaluation, now: datetime
) -> tuple[dict[str, Any], int, list[dict]] | None | str:
    """The next (config, chunk index, chunk) to run, None when every chunk is
    done, or "waiting" when the remaining chunks' models are all cooling."""
    progress = evaluation.progress
    waiting = False
    for config in evaluation.configs:
        state = progress[config["key"]]
        for index, chunk in enumerate(_chunks(evaluation, config)):
            if index in state["done"]:
                continue
            if _cooling(progress, config["model"], now):
                waiting = True
                break
            return config, index, chunk
    return "waiting" if waiting else None


def _categorizer_for(config: dict[str, Any]) -> GroqCategorizer:
    settings = get_settings()
    return GroqCategorizer(
        settings.groq_api_key or "",
        config["model"],
        timeout=settings.gemini_timeout_seconds,
        min_interval_seconds=0.0,
        max_retries=0,
        description_chars=int(config["description_chars"]),
        prompt_layout=config["layout"],
    )


def run_evaluation_step(
    session_factory: Callable[[], Session],
    *,
    now: datetime | None = None,
    categorizer_factory: Callable[[dict[str, Any]], GroqCategorizer] = _categorizer_for,
) -> str | None:
    """Make at most one request for the running evaluation. Returns None when
    no evaluation is running (the caller then drains the label queue as
    usual), else a short word for what happened."""
    now = now or datetime.now(UTC)
    db = session_factory()
    try:
        evaluation = running_evaluation(db)
        if evaluation is None:
            return None
        step = _next_step(evaluation, now)
        if step is None:
            evaluation.report = build_report(evaluation)
            evaluation.status = DONE
            evaluation.finished_at = now
            db.commit()
            logger.info("AI label evaluation %d finished", evaluation.id)
            return "finished"
        if step == "waiting":
            return "waiting"
        config, index, chunk = step
        options = [
            _option(c) for c in db.query(EventCategory).filter(EventCategory.is_active.is_(True))
        ]
        items = [
            EventToClassify(
                id=e["id"], title=e["title"], description=e["description"],
                source_category=e["source_category"], venue=e["venue"],
            )
            for e in chunk
        ]
        progress = _copy(evaluation.progress)
        state = progress[config["key"]]
        categorizer = categorizer_factory(config)
        outcome = "ok"
        try:
            with categorizer:
                labels = categorizer.classify_batch(options, items)
        except GeminiQuotaExceeded as exc:
            wait = (exc.retry_after_seconds or 60.0) + 5.0
            progress.setdefault("_cooldowns", {})[config["model"]] = (
                now + timedelta(seconds=wait)
            ).isoformat()
            state["rate_limited"] += 1
            labels = None
            outcome = "rate_limited"
        except (GeminiBadOutput, GeminiError) as exc:
            tries = state["tries"].get(str(index), 0) + 1
            state["tries"][str(index)] = tries
            state["errors"].append(sanitize_error(f"chunk {index}: {exc}"))
            if tries >= MAX_CHUNK_TRIES:
                state["done"].append(index)
            labels = None
            outcome = "error"
        stats = categorizer.last_stats
        if outcome != "rate_limited":
            state["requests"] += 1
            evaluation.requests_made = (evaluation.requests_made or 0) + 1
        for key, value in (
            ("prompt", stats.prompt_tokens), ("completion", stats.completion_tokens),
            ("total", stats.total_tokens), ("cached", stats.cached_tokens),
            ("reasoning", stats.reasoning_tokens),
        ):
            state["tokens"][key] += value or 0
        if labels is not None:
            for event_id, label in labels.items():
                state["labels"][str(event_id)] = [label.category, label.keep]
            state["done"].append(index)
        evaluation.progress = progress
        db.commit()
        return outcome
    except Exception as exc:  # noqa: BLE001 - a broken evaluation must not stop the drain
        db.rollback()
        evaluation = running_evaluation(db)
        if evaluation is not None:
            evaluation.status = FAILED
            evaluation.error = sanitize_error(str(exc))
            evaluation.finished_at = now
            db.commit()
        logger.warning("AI label evaluation failed: %s", exc)
        return "failed"
    finally:
        db.close()


def _option(category: EventCategory):
    from app.services.ai_categorizer import CategoryOption

    return CategoryOption(slug=category.slug, name=category.name, description=category.description)


def _copy(value: Any) -> Any:
    import copy

    return copy.deepcopy(value)


# --- Report ---------------------------------------------------------------------


def _pct(part: int, whole: int) -> float | None:
    return round(100.0 * part / whole, 1) if whole else None


def progress_summary(evaluation: AiLabelEvaluation) -> list[dict[str, Any]]:
    rows = []
    for config in evaluation.configs:
        state = evaluation.progress.get(config["key"], {})
        total = len(_chunks(evaluation, config))
        rows.append({
            "key": config["key"],
            "model": config["model"],
            "chunks_done": len(set(state.get("done", []))),
            "chunks": total,
            "labeled": len(state.get("labels", {})),
            "rate_limited": state.get("rate_limited", 0),
        })
    return rows


def build_report(evaluation: AiLabelEvaluation) -> dict[str, Any]:
    sample = {str(e["id"]): e for e in evaluation.sample}
    configs = evaluation.configs
    base_key = configs[0]["key"]
    base = evaluation.progress[base_key]["labels"]

    def token_stats(key: str) -> dict[str, Any]:
        state = evaluation.progress[key]
        labeled = len(state["labels"])
        tokens = state["tokens"]
        return {
            "requests": state["requests"],
            "labeled": labeled,
            "unanswered": len(sample) - labeled,
            "tokens": tokens,
            "tokens_per_event": round(tokens["total"] / labeled) if labeled else None,
            "uncached_tokens_per_event": (
                round((tokens["total"] - tokens["cached"]) / labeled) if labeled else None
            ),
            "errors": state["errors"][-10:],
        }

    def override_accuracy(labels: dict[str, list]) -> dict[str, Any]:
        rows = [
            (eid, e["override_category"]) for eid, e in sample.items()
            if e.get("override_category") and eid in labels
        ]
        right = sum(1 for eid, truth in rows if labels[eid][0] == truth)
        return {"events": len(rows), "matching": right, "pct": _pct(right, len(rows))}

    # Noise floor: the baseline rerun against the labels already stored on the
    # events (same model and prompt), so a candidate is never held to a higher
    # bar than the baseline meets against itself.
    stored = [
        (eid, e) for eid, e in sample.items() if e.get("stored_category") and eid in base
    ]
    stored_cat = sum(1 for eid, e in stored if base[eid][0] == e["stored_category"])
    stored_keep = sum(1 for eid, e in stored if bool(base[eid][1]) == bool(e["stored_keep"]))

    report: dict[str, Any] = {
        "sample_size": len(sample),
        "bucket_counts": _bucket_counts(sample.values()),
        "baseline": {
            "config": configs[0],
            **token_stats(base_key),
            "override_accuracy": override_accuracy(base),
            "hidden": sum(1 for v in base.values() if not v[1]),
            "agreement_with_stored_labels": {
                "events": len(stored),
                "category_pct": _pct(stored_cat, len(stored)),
                "keep_pct": _pct(stored_keep, len(stored)),
            },
        },
        "candidates": [],
    }
    for config in configs[1:]:
        labels = evaluation.progress[config["key"]]["labels"]
        both = [eid for eid in sample if eid in base and eid in labels]
        cat_agree = sum(1 for eid in both if base[eid][0] == labels[eid][0])
        keep_agree = sum(1 for eid in both if bool(base[eid][1]) == bool(labels[eid][1]))
        false_hides = [eid for eid in both if base[eid][1] and not labels[eid][1]]
        extra_keeps = [eid for eid in both if not base[eid][1] and labels[eid][1]]
        disagreements = [
            {
                "id": int(eid),
                "title": sample[eid]["title"],
                "buckets": sample[eid]["buckets"],
                "baseline": {"category": base[eid][0], "keep": bool(base[eid][1])},
                "candidate": {"category": labels[eid][0], "keep": bool(labels[eid][1])},
                "override_category": sample[eid].get("override_category"),
            }
            for eid in both
            if base[eid][0] != labels[eid][0] or bool(base[eid][1]) != bool(labels[eid][1])
        ]
        by_bucket = {}
        for bucket in report["bucket_counts"]:
            ids = [eid for eid in both if bucket in sample[eid]["buckets"]]
            by_bucket[bucket] = {
                "events": len(ids),
                "category_pct": _pct(
                    sum(1 for eid in ids if base[eid][0] == labels[eid][0]), len(ids)
                ),
                "keep_pct": _pct(
                    sum(1 for eid in ids if bool(base[eid][1]) == bool(labels[eid][1])),
                    len(ids),
                ),
            }
        category_pct = _pct(cat_agree, len(both))
        keep_pct = _pct(keep_agree, len(both))
        report["candidates"].append({
            "config": config,
            **token_stats(config["key"]),
            "compared": len(both),
            "category_agreement_pct": category_pct,
            "keep_agreement_pct": keep_pct,
            "false_hides": len(false_hides),
            "extra_keeps": len(extra_keeps),
            "override_accuracy": override_accuracy(labels),
            "by_bucket": by_bucket,
            # A first screen only: every disagreement still needs a person's look.
            "meets_95_pct": bool(
                category_pct is not None and keep_pct is not None
                and category_pct >= 95.0 and keep_pct >= 95.0
            ),
            "disagreements": disagreements,
        })
    return report


def _bucket_counts(items) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        for bucket in item["buckets"]:
            counts[bucket] = counts.get(bucket, 0) + 1
    return dict(sorted(counts.items()))
