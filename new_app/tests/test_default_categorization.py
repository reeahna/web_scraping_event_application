"""Starter categorization rules and the import-time assign_category hook."""

from __future__ import annotations

from app.models.categorization_rule import CategorizationRule
from app.services.categorization import assign_category, seed_rules


def test_seed_rules_creates_defaults_and_is_idempotent(db_session):
    created = seed_rules(db_session)
    assert created > 0
    assert db_session.query(CategorizationRule).count() == created
    # Running again is a no-op, so administrator edits are never overwritten.
    assert seed_rules(db_session) == 0
    assert db_session.query(CategorizationRule).count() == created


def test_assign_category_is_a_noop_when_no_rules_exist(db_session, make_city, make_event):
    event = make_event(make_city(), title="Live Jazz Concert")
    assert assign_category(db_session, event) is None
    assert event.category_id is None


def test_seeded_rules_categorize_by_keyword(db_session, make_city, make_event):
    seed_rules(db_session)
    city = make_city()

    concert = make_event(city, title="Live Jazz Concert at the Bluebird")
    assert assign_category(db_session, concert).category.slug == "music"

    tasting = make_event(city, title="Downtown Wine Tasting")
    assert assign_category(db_session, tasting).category.slug == "food-and-drink"

    # A distinctive category (Music) wins over a broad one (Community) when both
    # keywords are present.
    festival = make_event(city, title="Summer Music Festival")
    assert assign_category(db_session, festival).category.slug == "music"


def test_uncategorizable_event_falls_back_to_other(db_session, make_city, make_event):
    seed_rules(db_session)
    event = make_event(make_city(), title="Zxqvbnm")
    result = assign_category(db_session, event)
    assert result is not None
    assert result.category.slug == "other"
