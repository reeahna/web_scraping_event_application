"""Visitor log: counting public page views by town, without cookies or stored
IP addresses, and the admin page that shows them."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.models.site_visit import SitePageDay, SiteVisitorDay, VisitSalt
from app.repositories.public_events import current_public_date
from app.services import site_visits

BROWSER = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130.0 Safari/537.36"
PHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) Mobile/15E148 Safari/604.1"
TOMORROW = datetime.now(UTC).date() + timedelta(days=1)


@pytest.fixture
def tracking(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "visit_tracking_enabled", True)
    # Flush only when asked, so each test decides when counts reach the DB.
    monkeypatch.setattr(settings, "visit_flush_seconds", 3600)
    site_visits.reset_for_tests()
    yield
    site_visits.reset_for_tests()


def _visit(client, path, ua=BROWSER, ip="203.0.113.7", **headers):
    return client.get(path, headers={"User-Agent": ua, "X-Forwarded-For": ip, **headers})


def _visitors(db_session):
    db_session.expire_all()
    return db_session.scalars(select(SiteVisitorDay)).all()


class TestCounting:
    def test_town_page_views_are_counted_per_visitor(self, tracking, client, make_city,
                                                     db_session):
        make_city(name="Ann Arbor", slug="ann-arbor")
        make_city(name="Ithaca", slug="ithaca")
        _visit(client, "/city/ann-arbor")
        _visit(client, "/city/ann-arbor?page=2")
        _visit(client, "/city/ithaca")
        _visit(client, "/city/ithaca", ua=PHONE, ip="198.51.100.4",
               Referer="https://www.google.com/search?q=events")
        site_visits.flush()

        rows = sorted(_visitors(db_session), key=lambda r: r.page_views)
        assert [r.page_views for r in rows] == [1, 3]
        phone, laptop = rows
        assert laptop.towns == "ann-arbor,ithaca"
        assert laptop.device == "desktop" and laptop.referrer_host is None
        assert phone.towns == "ithaca"
        assert phone.device == "mobile" and phone.referrer_host == "google.com"

        pages = {p.path: p for p in db_session.scalars(select(SitePageDay))}
        assert pages["/city/ann-arbor"].views == 2
        assert pages["/city/ann-arbor"].town_slug == "ann-arbor"
        assert pages["/city/ithaca"].views == 2

    def test_no_ip_or_cookie_is_stored(self, tracking, client, make_city, db_session):
        make_city(slug="ithaca")
        response = _visit(client, "/city/ithaca", ip="203.0.113.99")
        site_visits.flush()
        cookies = " ".join(response.headers.get_list("set-cookie"))
        assert "visit" not in cookies.lower()
        (row,) = _visitors(db_session)
        assert "203.0.113.99" not in repr(vars(row))
        assert len(row.visitor_hash) == 32

    def test_old_salts_are_deleted(self, tracking, db_session):
        today = current_public_date()
        db_session.add(VisitSalt(day=today - timedelta(days=1), salt="x" * 64))
        db_session.commit()
        site_visits.visitor_hash(today, "1.2.3.4", BROWSER)
        db_session.expire_all()
        assert [s.day for s in db_session.scalars(select(VisitSalt))] == [today]

    def test_event_page_counts_toward_its_town(self, tracking, client, make_city,
                                               make_website, make_event, db_session):
        city = make_city(name="Ithaca", slug="ithaca")
        website = make_website(city, is_active=True, approved_pattern={"pattern_name": "x"})
        event = make_event(city, website=website, start_date=TOMORROW)
        assert _visit(client, f"/events/{event.id}").status_code == 200
        site_visits.flush()
        (row,) = _visitors(db_session)
        assert row.towns == "ithaca"

    def test_weekend_and_today_pages_count_toward_the_town(self, tracking, client,
                                                          make_city, db_session):
        make_city(name="Ithaca", slug="ithaca")
        assert _visit(client, "/city/ithaca/this-weekend").status_code == 200
        assert _visit(client, "/city/ithaca/today").status_code == 200
        site_visits.flush()
        (row,) = _visitors(db_session)
        assert row.towns == "ithaca" and row.page_views == 2

    @pytest.mark.parametrize(
        "ua",
        ["Googlebot/2.1 (+http://www.google.com/bot.html)", "curl/8.4.0",
         "python-httpx/0.27", ""],
    )
    def test_bots_and_scripts_are_not_counted(self, tracking, client, make_city, db_session,
                                              ua):
        make_city(slug="ithaca")
        _visit(client, "/city/ithaca", ua=ua)
        site_visits.flush()
        assert _visitors(db_session) == []

    def test_admin_static_head_missing_and_prefetch_are_not_counted(
        self, tracking, client, make_city, db_session
    ):
        make_city(slug="ithaca")
        _visit(client, "/static/style.css")
        _visit(client, "/health/live")
        _visit(client, "/city/nowhere")  # 404
        client.head("/city/ithaca", headers={"User-Agent": BROWSER})
        _visit(client, "/city/ithaca", **{"Sec-Purpose": "prefetch"})
        site_visits.flush()
        assert _visitors(db_session) == []

    def test_signed_in_visitor_is_named(self, tracking, client, login, make_city,
                                        db_session):
        make_city(slug="ithaca")
        login("reader@example.com")
        _visit(client, "/city/ithaca")
        site_visits.flush()
        rows = [r for r in _visitors(db_session) if r.user_id is not None]
        assert len(rows) == 1

    def test_disabled_counts_nothing(self, client, make_city, db_session):
        make_city(slug="ithaca")
        _visit(client, "/city/ithaca")
        site_visits.flush()
        assert _visitors(db_session) == []

    def test_old_rows_are_pruned(self, tracking, db_session):
        old = current_public_date() - timedelta(days=get_settings().visit_retention_days + 1)
        db_session.add(SiteVisitorDay(day=old, visitor_hash="a" * 32, page_views=1,
                                      device="desktop"))
        db_session.add(SitePageDay(day=old, path="/", views=1))
        db_session.commit()
        site_visits.flush()
        assert _visitors(db_session) == []
        assert db_session.scalars(select(SitePageDay)).all() == []

    def test_counts_add_up_across_flushes(self, tracking, client, make_city, db_session):
        make_city(slug="ithaca")
        make_city(name="Ann Arbor", slug="ann-arbor")
        _visit(client, "/city/ithaca")
        site_visits.flush()
        _visit(client, "/city/ann-arbor")
        site_visits.flush()
        (row,) = _visitors(db_session)
        assert row.page_views == 2
        assert row.towns == "ithaca,ann-arbor"


class TestAdminPage:
    def test_requires_permission(self, client, login, make_user):
        make_user("reader@example.com")
        login("reader@example.com")
        response = client.get("/admin/visitors", follow_redirects=False)
        assert response.status_code in (302, 303, 403)

    def test_shows_towns_visitors_and_sign_ins(self, tracking, client, login,
                                               make_super_admin, make_city):
        make_city(name="Ithaca", slug="ithaca", state_or_region="NY")
        _visit(client, "/city/ithaca", ua=PHONE, ip="198.51.100.4")
        make_super_admin("boss@example.com")
        login("boss@example.com")
        page = client.get("/admin/visitors?days=30", headers={"User-Agent": BROWSER})
        assert page.status_code == 200
        text = page.text
        assert "Towns people are looking at" in text
        assert "Ithaca, NY" in text
        assert "Anonymous visitor" in text
        assert "Mobile" in text
        assert "Signed in" in text  # the admin's own sign-in

    def test_bad_range_falls_back(self, client, login, make_super_admin):
        make_super_admin("boss@example.com")
        login("boss@example.com")
        assert client.get("/admin/visitors?days=abc").status_code == 200
        assert client.get("/admin/visitors?days=5000&page=-3").status_code == 200

    def test_linked_from_admin_nav(self, client, login, make_super_admin):
        make_super_admin("boss@example.com")
        login("boss@example.com")
        assert 'href="/admin/visitors"' in client.get("/admin/reports").text
