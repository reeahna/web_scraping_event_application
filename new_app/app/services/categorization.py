import re
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.safe_regex import validate_safe_regex
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
    return validate_safe_regex(cleaned)


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


def _set_category_fields(event: Event, result: CategorizationResult) -> None:
    event.category_id = result.category.id if result.category else None
    event.categorization_rule_id = result.rule_id
    event.category_source = result.rule_type or (
        "fallback" if result.fallback_used else "uncategorized"
    )


def _active_rules_exist(db: Session) -> bool:
    return (
        db.query(CategorizationRule.id).filter(CategorizationRule.is_active.is_(True)).first()
        is not None
    )


def assign_category(db: Session, event: Event) -> CategorizationResult | None:
    """Set an event's category from the active rules, without committing (the
    caller owns the transaction). Used from the import pipeline. When no rules
    are configured at all it leaves the event unchanged: with none, everything
    would just become 'Other', so skipping keeps a rule-less deployment behaving
    exactly as it did before categorization existed."""
    if not _active_rules_exist(db):
        return None
    result = categorize_event(db, event)
    _set_category_fields(event, result)
    return result


def apply_categorization(db: Session, event: Event) -> CategorizationResult:
    result = categorize_event(db, event)
    _set_category_fields(event, result)
    db.commit()
    db.refresh(event)
    return result


# Starter keyword rules mapping common event language to the seeded categories.
# Each is a case-insensitive regex over the event's title, description and raw
# source category. Priorities break ties so a distinctive category (Music) wins
# over a broad one (Community) when both match. Administrators can edit or delete
# these in the Category Rules screen; they are only ever created when no rule
# exists yet, so edits are never overwritten.
DEFAULT_KEYWORD_RULES: tuple[tuple[str, str, str, int], ...] = (
    ("Music", "music",
     r"\b(concert|music|band|orchestra|symphony|philharmonic|jazz|blues|"
     r"choir|chorale|recital|acoustic|open mic|karaoke|songwriter|opera|dj)\b", 95),
    ("Sports", "sports",
     r"\b(game|match|tournament|race|marathon|5k|10k|running|cycling|basketball|"
     r"soccer|hockey|baseball|football|volleyball|golf|tennis|athletic)\b", 90),
    ("Food and drink", "food-and-drink",
     r"\b(food truck|dinner|brunch|tasting|wine|beer|brewery|brewing|cocktail|"
     r"culinary|dining|happy hour|bbq|barbecue|farmers market)\b", 88),
    ("Arts and culture", "arts-and-culture",
     r"\b(art|arts|gallery|exhibit|exhibition|museum|theatre|theater|play|drama|"
     r"dance|ballet|film|movie|cinema|screening|comedy|poetry|literary|author|"
     r"painting|sculpture)\b", 85),
    ("Family", "family",
     r"\b(kids|children|family|storytime|toddler|all ages|youth)\b", 82),
    ("Education", "education",
     r"\b(class|classes|workshop|seminar|lecture|course|training|tutorial|lesson|"
     r"webinar)\b", 78),
    ("Health and wellness", "health-and-wellness",
     r"\b(yoga|wellness|fitness|meditation|mindfulness|pilates|nutrition|"
     r"wellbeing)\b", 75),
    ("Nightlife", "nightlife",
     r"\b(nightlife|nightclub|club night|trivia|pub|bar crawl|drag|late night)\b", 70),
    ("Outdoors", "outdoors",
     r"\b(outdoor|hike|hiking|trail|nature|garden|camping|kayak|canoe|birding)\b", 65),
    ("Religious", "religious",
     r"\b(church|worship|mass|sermon|faith|prayer|bible|gospel|temple|synagogue|"
     r"spiritual|ministry)\b", 60),
    ("Community", "community",
     r"\b(festival|fair|market|community|parade|fundraiser|benefit|volunteer|"
     r"meetup|celebration|block party)\b", 55),
    ("Business", "business",
     r"\b(business|networking|conference|professional|entrepreneur|career|"
     r"startup|expo|summit)\b", 50),
    ("Government", "government",
     r"\b(city council|town hall|council meeting|government|public meeting|"
     r"election|civic)\b", 45),
)


def seed_rules(db: Session) -> int:
    """Create the starter keyword rules when the deployment has none. Idempotent:
    a no-op once any rule exists. Returns the number of rules created."""
    if db.query(CategorizationRule.id).first() is not None:
        return 0
    categories = {category.slug: category for category in db.query(EventCategory).all()}
    created = 0
    for name, slug, pattern, priority in DEFAULT_KEYWORD_RULES:
        category = categories.get(slug)
        if category is None:
            continue
        db.add(
            CategorizationRule(
                name=name,
                rule_type="keyword",
                category_id=category.id,
                is_active=True,
                priority=priority,
                pattern=pattern,
                is_regex=True,
                case_sensitive=False,
            )
        )
        created += 1
    db.commit()
    return created
