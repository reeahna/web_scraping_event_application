import pytest

from app.core.categories import INITIAL_EVENT_CATEGORIES
from app.core.permissions import ADMINISTRATOR, EDITOR
from app.core.seed import seed_event_categories
from app.models.audit_log import AuditLog
from app.models.categorization_rule import CategorizationRule
from app.models.event_category import EventCategory
from app.services.categorization import (
    CONFIDENCE_TYPES,
    apply_categorization,
    categorize_event,
    validate_rule_pattern,
)


def _csrf(client) -> str:
    return client.cookies.get("csrf_token")


def _admin(make_user, login, *, role=ADMINISTRATOR, email="category-admin@example.com"):
    user = make_user(email=email, password="category-pass-123", role_name=role)
    login(email, "category-pass-123")
    return user


def _category(db_session, slug: str) -> EventCategory:
    return db_session.query(EventCategory).filter(EventCategory.slug == slug).one()


def _rule(db_session, *, name, rule_type, category, **values):
    rule = CategorizationRule(
        name=name,
        rule_type=rule_type,
        category_id=category.id,
        is_active=values.pop("is_active", True),
        priority=values.pop("priority", 0),
        **values,
    )
    db_session.add(rule)
    db_session.commit()
    db_session.refresh(rule)
    return rule


def test_initial_categories_seeded_without_assuming_ids(db_session):
    categories = db_session.query(EventCategory).all()
    assert {category.name for category in categories} == {
        name for name, _slug in INITIAL_EVENT_CATEGORIES
    }
    assert len({category.id for category in categories}) == len(INITIAL_EVENT_CATEGORIES)


def test_category_seeding_is_idempotent(db_session):
    before = db_session.query(EventCategory).count()
    seed_event_categories(db_session)
    seed_event_categories(db_session)
    assert db_session.query(EventCategory).count() == before


def test_category_create_update_activate_and_deactivate(client, make_user, login, db_session):
    _admin(make_user, login)
    created = client.post(
        "/admin/event-categories",
        data={
            "name": "Technology",
            "description": "Technology events",
            "display_order": 20,
            "csrf_token": _csrf(client),
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    category = db_session.query(EventCategory).filter(EventCategory.slug == "technology").one()

    updated = client.post(
        f"/admin/event-categories/{category.id}",
        data={
            "name": "Technology and Science",
            "description": "Updated",
            "display_order": 21,
            "csrf_token": _csrf(client),
        },
        follow_redirects=False,
    )
    assert updated.status_code == 303
    db_session.refresh(category)
    assert category.slug == "technology-and-science"

    for action, expected in (("deactivate", False), ("activate", True)):
        response = client.post(
            f"/admin/event-categories/{category.id}/status",
            data={"action": action, "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert response.status_code == 303
        db_session.refresh(category)
        assert category.is_active is expected

    actions = {entry.action for entry in db_session.query(AuditLog).all()}
    assert {
        "category_created",
        "category_updated",
        "category_deactivated",
        "category_activated",
    }.issubset(actions)


def test_referenced_category_deletion_blocked(
    client, make_user, make_city, make_event, login, db_session
):
    _admin(make_user, login)
    category = _category(db_session, "music")
    make_event(make_city(), category=category)
    response = client.post(
        f"/admin/event-categories/{category.id}/delete",
        data={"csrf_token": _csrf(client)},
    )
    assert response.status_code == 409


def test_unreferenced_category_can_be_deleted(client, make_user, make_category, login, db_session):
    _admin(make_user, login)
    category = make_category(name="Temporary Category", slug="temporary-category")
    category_id = category.id
    response = client.post(
        f"/admin/event-categories/{category.id}/delete",
        data={"csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    db_session.expire_all()
    assert db_session.get(EventCategory, category_id) is None


def test_editor_cannot_manage_categories(client, make_user, login):
    _admin(make_user, login, role=EDITOR, email="category-editor@example.com")
    assert client.get("/admin/event-categories").status_code == 403


def test_exact_source_mapping_wins_over_lower_precedence_rules(db_session, make_city, make_event):
    music = _category(db_session, "music")
    sports = _category(db_session, "sports")
    exact = _rule(
        db_session,
        name="Exact Concert",
        rule_type="source_mapping",
        category=music,
        source_category_value="Concert",
    )
    _rule(
        db_session,
        name="Keyword Concert",
        rule_type="keyword",
        category=sports,
        pattern="concert",
        priority=999,
    )
    event = make_event(make_city(), title="Concert and sports", source_category="Concert")
    result = categorize_event(db_session, event)
    assert result.category.id == music.id
    assert result.rule_id == exact.id
    assert result.confidence_type == "exact"


def test_administrator_mapping_and_structured_result(db_session, make_city, make_event):
    category = _category(db_session, "education")
    rule = _rule(
        db_session,
        name="Admin Workshop Mapping",
        rule_type="administrator_mapping",
        category=category,
        source_category_value="Workshop",
    )
    event = make_event(make_city(), source_category="workshop")
    result = categorize_event(db_session, event)
    assert result.rule_name == rule.name
    assert result.rule_type == "administrator_mapping"
    assert result.confidence_type in CONFIDENCE_TYPES
    assert result.explanation
    assert result.manual_review_recommended is False
    assert result.fallback_used is False


def test_website_specific_mapping(db_session, make_city, make_website, make_event):
    city = make_city()
    website = make_website(city)
    category = _category(db_session, "community")
    _rule(
        db_session,
        name="Website Community",
        rule_type="website_mapping",
        category=category,
        website_id=website.id,
        source_category_value="Gathering",
    )
    event = make_event(city, website=website, source_category="Gathering")
    assert categorize_event(db_session, event).category.id == category.id


@pytest.mark.parametrize(
    ("rule_type", "field_values", "category_slug", "manual_review"),
    [
        ("venue", {"pattern": "Convention Center"}, "business", False),
        ("keyword", {"pattern": "jazz"}, "music", True),
    ],
)
def test_venue_and_keyword_rules(
    db_session,
    make_city,
    make_event,
    rule_type,
    field_values,
    category_slug,
    manual_review,
):
    category = _category(db_session, category_slug)
    _rule(
        db_session,
        name=f"{rule_type} rule",
        rule_type=rule_type,
        category=category,
        **field_values,
    )
    event = make_event(
        make_city(), title="Evening Jazz Showcase", venue="Downtown Convention Center"
    )
    result = categorize_event(db_session, event)
    assert result.category.id == category.id
    assert result.manual_review_recommended is manual_review


def test_priority_ordering_within_rule_type(db_session, make_city, make_event):
    music = _category(db_session, "music")
    nightlife = _category(db_session, "nightlife")
    _rule(
        db_session,
        name="Low Priority Jazz",
        rule_type="keyword",
        category=music,
        pattern="jazz",
        priority=1,
    )
    high = _rule(
        db_session,
        name="High Priority Jazz",
        rule_type="keyword",
        category=nightlife,
        pattern="jazz",
        priority=10,
    )
    event = make_event(make_city(), title="Jazz Night")
    assert categorize_event(db_session, event).rule_id == high.id


def test_inactive_rules_ignored_and_fallback_used(db_session, make_city, make_event):
    music = _category(db_session, "music")
    _rule(
        db_session,
        name="Inactive Match",
        rule_type="keyword",
        category=music,
        pattern="music",
        is_active=False,
    )
    event = make_event(make_city(), title="Music Event")
    result = categorize_event(db_session, event)
    assert result.category.slug == "other"
    assert result.confidence_type == "fallback"
    assert result.fallback_used is True
    assert result.manual_review_recommended is True


def test_uncategorized_when_fallback_inactive(db_session, make_city, make_event):
    other = _category(db_session, "other")
    other.is_active = False
    db_session.commit()
    result = categorize_event(db_session, make_event(make_city()))
    assert result.category is None
    assert result.confidence_type == "uncategorized"


def test_repeated_categorization_is_identical(db_session, make_city, make_event):
    event = make_event(make_city(), title="No matching deterministic rule")
    first = categorize_event(db_session, event)
    second = categorize_event(db_session, event)
    assert first == second


def test_apply_categorization_does_not_clear_manual_override(db_session, make_city, make_event):
    base = _category(db_session, "music")
    override = _category(db_session, "sports")
    event = make_event(make_city(), category=base)
    event.category_override_id = override.id
    db_session.commit()
    apply_categorization(db_session, event)
    assert event.category_override_id == override.id
    assert event.effective_category.id == override.id


@pytest.mark.parametrize(
    "pattern",
    [
        "(",
        "(?=danger)",
        "x" * 201,
        # Nested-quantifier / alternation shapes that cause catastrophic
        # backtracking (ReDoS) in Python's backtracking regex engine.
        "(a+)+$",
        "(a*)*",
        "(a|ab)+$",
    ],
)
def test_invalid_or_high_risk_regex_rejected(pattern):
    with pytest.raises(ValueError):
        validate_rule_pattern(pattern, is_regex=True)


@pytest.mark.parametrize(
    "pattern", ["a*?", "a++", "concert|music", r"\bjazz\b", r"\d{4}-\d{2}-\d{2}"]
)
def test_safe_regex_patterns_accepted(pattern):
    # These are ordinary, non-backtracking-prone patterns — the safety filter
    # must not reject them. "a*?" is the standard *lazy* quantifier; "a++" is
    # a *possessive* quantifier (supported since Python 3.11) that actively
    # prevents backtracking rather than causing it. Both were previously
    # rejected by an overly broad "any quantifier immediately followed by
    # another quantifier character" check that conflated these safe suffixes
    # with the genuinely dangerous nested-quantifier-on-a-group shape above.
    assert validate_rule_pattern(pattern, is_regex=True) == pattern


def test_rule_creation_invalid_regex_rejected(client, make_user, login, db_session):
    _admin(make_user, login)
    category = _category(db_session, "music")
    response = client.post(
        "/admin/categorization-rules",
        data={
            "name": "Bad Regex",
            "rule_type": "keyword",
            "category_id": category.id,
            "priority": 1,
            "website_id": "",
            "source_category_value": "",
            "pattern": "(",
            "is_regex": "on",
            "csrf_token": _csrf(client),
        },
    )
    assert response.status_code == 422


def test_rule_creation_and_status_are_audited(client, make_user, login, db_session):
    _admin(make_user, login)
    category = _category(db_session, "music")
    created = client.post(
        "/admin/categorization-rules",
        data={
            "name": "Audited Keyword",
            "rule_type": "keyword",
            "category_id": category.id,
            "priority": 2,
            "website_id": "",
            "source_category_value": "",
            "pattern": "concert",
            "csrf_token": _csrf(client),
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    rule = db_session.query(CategorizationRule).filter_by(name="Audited Keyword").one()
    updated = client.post(
        f"/admin/categorization-rules/{rule.id}",
        data={
            "name": "Updated Audited Keyword",
            "rule_type": "keyword",
            "category_id": category.id,
            "priority": 9,
            "website_id": "",
            "source_category_value": "",
            "pattern": "live concert",
            "csrf_token": _csrf(client),
        },
        follow_redirects=False,
    )
    assert updated.status_code == 303
    db_session.refresh(rule)
    assert (rule.name, rule.priority, rule.pattern) == (
        "Updated Audited Keyword",
        9,
        "live concert",
    )
    status = client.post(
        f"/admin/categorization-rules/{rule.id}/status",
        data={"action": "deactivate", "csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert status.status_code == 303
    actions = {entry.action for entry in db_session.query(AuditLog).all()}
    assert {
        "categorization_rule_created",
        "categorization_rule_updated",
        "categorization_rule_deactivated",
    }.issubset(actions)
