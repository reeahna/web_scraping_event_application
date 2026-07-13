import re
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models.categorization_rule import CategorizationRule
from app.models.event import Event
from app.models.event_category import EventCategory

RULE_TYPES = (
    "source_mapping",
    "administrator_mapping",
    "website_mapping",
    "venue",
    "keyword",
)
CONFIDENCE_TYPES = ("exact", "strong_rule", "weak_rule", "fallback", "uncategorized")
_PRECEDENCE = {rule_type: index for index, rule_type in enumerate(RULE_TYPES)}
_UNSAFE_REGEX_RE = re.compile(
    r"\(\?"  # lookaround, named, or non-capturing groups — "(?=", "(?:", "(?P<...>", etc.
    r"|\\[1-9]"  # backreferences
    r"|\([^)]*[+*|][^)]*\)\s*[+*{]"  # a group containing +, *, or | that is itself
    # quantified, e.g. "(a+)+" or "(a|ab)+" — the classic nested-quantifier/
    # alternation shape that causes catastrophic backtracking in Python's `re`.
)


@dataclass(frozen=True)
class CategorizationResult:
    category: EventCategory | None
    rule_id: int | None
    rule_name: str | None
    rule_type: str | None
    confidence_type: str
    explanation: str
    manual_review_recommended: bool
    fallback_used: bool


def validate_rule_pattern(pattern: str | None, *, is_regex: bool) -> str | None:
    cleaned = (pattern or "").strip() or None
    if not is_regex or cleaned is None:
        return cleaned
    if len(cleaned) > 200:
        raise ValueError("Regular expressions must be 200 characters or fewer")
    if _UNSAFE_REGEX_RE.search(cleaned):
        raise ValueError("Regular expression uses a disallowed high-risk construct")
    try:
        re.compile(cleaned)
    except re.error as exc:
        raise ValueError(f"Invalid regular expression: {exc}") from exc
    return cleaned


def _text_matches(value: str | None, rule: CategorizationRule) -> bool:
    if not value or not rule.pattern:
        return False
    if rule.is_regex:
        flags = 0 if rule.case_sensitive else re.IGNORECASE
        return re.search(rule.pattern, value, flags=flags) is not None
    needle = rule.pattern if rule.case_sensitive else rule.pattern.casefold()
    haystack = value if rule.case_sensitive else value.casefold()
    return needle in haystack


def _rule_matches(event: Event, rule: CategorizationRule) -> bool:
    source_category = (event.source_category or "").strip()
    expected_source = (rule.source_category_value or "").strip()
    if not rule.case_sensitive:
        source_category = source_category.casefold()
        expected_source = expected_source.casefold()

    if rule.rule_type in {"source_mapping", "administrator_mapping"}:
        return bool(expected_source) and source_category == expected_source
    if rule.rule_type == "website_mapping":
        if rule.website_id != event.website_id:
            return False
        return not expected_source or source_category == expected_source
    if rule.rule_type == "venue":
        return _text_matches(event.venue, rule)
    if rule.rule_type == "keyword":
        searchable = " ".join(
            value for value in (event.title, event.description, event.source_category) if value
        )
        return _text_matches(searchable, rule)
    return False


def categorize_event(db: Session, event: Event) -> CategorizationResult:
    rules = db.query(CategorizationRule).filter(CategorizationRule.is_active.is_(True)).all()
    rules.sort(key=lambda rule: (_PRECEDENCE.get(rule.rule_type, 999), -rule.priority, rule.id))
    for rule in rules:
        if not rule.category.is_active or not _rule_matches(event, rule):
            continue
        confidence = {
            "source_mapping": "exact",
            "administrator_mapping": "strong_rule",
            "website_mapping": "strong_rule",
            "venue": "strong_rule",
            "keyword": "weak_rule",
        }[rule.rule_type]
        return CategorizationResult(
            category=rule.category,
            rule_id=rule.id,
            rule_name=rule.name,
            rule_type=rule.rule_type,
            confidence_type=confidence,
            explanation=f"Rule '{rule.name}' matched using {rule.rule_type.replace('_', ' ')}.",
            manual_review_recommended=confidence == "weak_rule",
            fallback_used=False,
        )

    fallback = (
        db.query(EventCategory)
        .filter(EventCategory.slug == "other", EventCategory.is_active.is_(True))
        .first()
    )
    if fallback:
        return CategorizationResult(
            category=fallback,
            rule_id=None,
            rule_name=None,
            rule_type=None,
            confidence_type="fallback",
            explanation="No active categorization rule matched; using Other.",
            manual_review_recommended=True,
            fallback_used=True,
        )
    return CategorizationResult(
        category=None,
        rule_id=None,
        rule_name=None,
        rule_type=None,
        confidence_type="uncategorized",
        explanation="No active categorization rule matched and no active fallback exists.",
        manual_review_recommended=True,
        fallback_used=False,
    )


def apply_categorization(db: Session, event: Event) -> CategorizationResult:
    result = categorize_event(db, event)
    event.category_id = result.category.id if result.category else None
    event.categorization_rule_id = result.rule_id
    event.category_source = result.rule_type or (
        "fallback" if result.fallback_used else "uncategorized"
    )
    db.commit()
    db.refresh(event)
    return result
