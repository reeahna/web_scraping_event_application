from datetime import UTC, datetime, timedelta

from app.core.permissions import ADMINISTRATOR, REGISTERED_USER
from app.repositories.public_events import PUBLIC_EVENTS_PER_PAGE

TODAY = datetime.now(UTC).date()
TOMORROW = TODAY + timedelta(days=1)
YESTERDAY = TODAY - timedelta(days=1)


def _visible_website(make_city, make_website, **city_kwargs):
    city = make_city(**city_kwargs) if city_kwargs else make_city()
    website = make_website(city, is_active=True, approved_pattern={"pattern_name": "static_html"})
    return city, website


def _visible_event(make_city, make_website, make_event, **event_overrides):
    city, website = _visible_website(make_city, make_website)
    event = make_event(
        city,
        website=website,
        start_date=event_overrides.pop("start_date", TOMORROW),
        **event_overrides,
    )
    return city, website, event


class TestHomepageAccess:
    def test_anonymous_gets_200(self, client):
        response = client.get("/")
        assert response.status_code == 200

    def test_registered_user_gets_200(self, client, make_user, login):
        make_user(email="reg@example.com", password="password12345", role_name=REGISTERED_USER)
        login("reg@example.com", "password12345")
        response = client.get("/")
        assert response.status_code == 200

    def test_visible_event_appears_on_homepage(self, client, make_city, make_website, make_event):
        _, _, event = _visible_event(make_city, make_website, make_event, title="Visible Event")
        response = client.get("/")
        assert response.status_code == 200
        assert "Visible Event" in response.text


class TestVisibilityMatrix:
    def test_archived_event_hidden(self, client, make_city, make_website, make_event):
        city, website = _visible_website(make_city, make_website)
        event = make_event(
            city, website=website, title="Archived Event", start_date=TOMORROW, archived=True
        )
        assert client.get(f"/events/{event.id}").status_code == 404
        assert "Archived Event" not in client.get("/").text

    def test_confirmed_duplicate_hidden(self, client, make_city, make_website, make_event):
        _, _, event = _visible_event(
            make_city,
            make_website,
            make_event,
            title="Duplicate Event",
            duplicate_status="confirmed_duplicate",
        )
        assert client.get(f"/events/{event.id}").status_code == 404
        assert "Duplicate Event" not in client.get("/").text

    def test_possible_duplicate_stays_visible(self, client, make_city, make_website, make_event):
        _, _, event = _visible_event(
            make_city,
            make_website,
            make_event,
            title="Possible Duplicate Event",
            duplicate_status="possible_duplicate",
        )
        assert client.get(f"/events/{event.id}").status_code == 200
        assert "Possible Duplicate Event" in client.get("/").text

    def test_inactive_website_hidden(self, client, make_city, make_website, make_event):
        city = make_city()
        website = make_website(city, is_active=False, approved_pattern={"pattern_name": "x"})
        event = make_event(city, website=website, title="Inactive Site Event", start_date=TOMORROW)
        assert client.get(f"/events/{event.id}").status_code == 404
        assert "Inactive Site Event" not in client.get("/").text

    def test_unapproved_website_hidden(self, client, make_city, make_website, make_event):
        city = make_city()
        website = make_website(city, is_active=True, approved_pattern=None)
        event = make_event(
            city, website=website, title="Unapproved Site Event", start_date=TOMORROW
        )
        assert client.get(f"/events/{event.id}").status_code == 404
        assert "Unapproved Site Event" not in client.get("/").text

    def test_inactive_city_hidden(self, client, make_city, make_website, make_event):
        city = make_city(name="Inactive City", slug="inactive-city", is_active=False)
        website = make_website(city, is_active=True, approved_pattern={"pattern_name": "x"})
        event = make_event(city, website=website, title="Inactive City Event", start_date=TOMORROW)
        assert client.get(f"/events/{event.id}").status_code == 404
        assert "Inactive City Event" not in client.get("/").text

    def test_past_dated_event_hidden(self, client, make_city, make_website, make_event):
        city, website = _visible_website(make_city, make_website)
        event = make_event(
            city, website=website, title="Past Event", start_date=YESTERDAY, end_date=YESTERDAY
        )
        assert client.get(f"/events/{event.id}").status_code == 404
        assert "Past Event" not in client.get("/").text

    def test_ongoing_event_visible(self, client, make_city, make_website, make_event):
        """start_date in the past but end_date today/future stays visible."""
        _, _, event = _visible_event(
            make_city,
            make_website,
            make_event,
            title="Ongoing Event",
            start_date=YESTERDAY,
            end_date=TOMORROW,
        )
        assert client.get(f"/events/{event.id}").status_code == 200
        assert "Ongoing Event" in client.get("/").text


class TestFilters:
    def test_city_filter(self, client, make_city, make_website, make_event):
        city_a, website_a = _visible_website(make_city, make_website)
        city_b = make_city(name="City B", slug="city-b")
        website_b = make_website(city_b, is_active=True, approved_pattern={"pattern_name": "x"})
        make_event(city_a, website=website_a, title="Event A", start_date=TOMORROW)
        make_event(city_b, website=website_b, title="Event B", start_date=TOMORROW)

        response = client.get(f"/?city_id={city_a.id}")
        assert "Event A" in response.text
        assert "Event B" not in response.text

    def test_category_filter(self, client, make_city, make_website, make_event, make_category):
        city, website = _visible_website(make_city, make_website)
        cat_a = make_category(name="Test Category A", slug="test-category-a")
        cat_b = make_category(name="Test Category B", slug="test-category-b")
        make_event(city, website=website, title="Cat A Event", start_date=TOMORROW, category=cat_a)
        make_event(city, website=website, title="Cat B Event", start_date=TOMORROW, category=cat_b)

        response = client.get(f"/?category_id={cat_a.id}")
        assert "Cat A Event" in response.text
        assert "Cat B Event" not in response.text

    def test_date_range_filter(self, client, make_city, make_website, make_event):
        city, website = _visible_website(make_city, make_website)
        near = TOMORROW
        far = TODAY + timedelta(days=30)
        make_event(city, website=website, title="Near Event", start_date=near)
        make_event(city, website=website, title="Far Event", start_date=far)

        response = client.get(f"/?date_to={near.isoformat()}")
        assert "Near Event" in response.text
        assert "Far Event" not in response.text

    def test_upcoming_only_excludes_events_starting_today(
        self, client, make_city, make_website, make_event
    ):
        city, website = _visible_website(make_city, make_website)
        make_event(city, website=website, title="Today Event", start_date=TODAY)
        make_event(city, website=website, title="Tomorrow Event", start_date=TOMORROW)

        default_response = client.get("/")
        assert "Today Event" in default_response.text
        assert "Tomorrow Event" in default_response.text

        upcoming_response = client.get("/?upcoming_only=1")
        assert "Today Event" not in upcoming_response.text
        assert "Tomorrow Event" in upcoming_response.text

    def test_invalid_query_params_ignored_safely(self, client):
        response = client.get("/?city_id=abc&category_id=xyz&page=nope&date_from=not-a-date")
        assert response.status_code == 200


class TestPagination:
    def test_pagination_has_next(self, client, make_city, make_website, make_event):
        city, website = _visible_website(make_city, make_website)
        for i in range(PUBLIC_EVENTS_PER_PAGE + 1):
            make_event(
                city,
                website=website,
                title=f"Page Event {i}",
                canonical_url=f"https://example.com/event-{i}",
                start_date=TOMORROW,
            )

        first_page = client.get("/")
        assert first_page.status_code == 200
        second_page = client.get("/?page=2")
        assert second_page.status_code == 200
        # The extra event only appears once we page forward.
        assert first_page.text != second_page.text


class TestEventDetailPage:
    def test_renders_public_fields(self, client, make_city, make_website, make_event):
        _, _, event = _visible_event(
            make_city,
            make_website,
            make_event,
            title="Detail Event",
            description="A great event",
            venue="Main Hall",
        )
        response = client.get(f"/events/{event.id}")
        assert response.status_code == 200
        assert "Detail Event" in response.text
        assert "A great event" in response.text
        assert "Main Hall" in response.text

    def test_hidden_event_returns_404(self, client, make_city, make_website, make_event):
        city, website = _visible_website(make_city, make_website)
        event = make_event(
            city, website=website, title="Hidden Detail Event", archived=True, start_date=TOMORROW
        )
        assert client.get(f"/events/{event.id}").status_code == 404

    def test_admin_block_hidden_for_anonymous(self, client, make_city, make_website, make_event):
        _, _, event = _visible_event(make_city, make_website, make_event)
        response = client.get(f"/events/{event.id}")
        assert f"/admin/events/{event.id}" not in response.text

    def test_admin_block_hidden_for_registered_user(
        self, client, make_city, make_website, make_event, make_user, login
    ):
        _, _, event = _visible_event(make_city, make_website, make_event)
        make_user(email="reg2@example.com", password="password12345", role_name=REGISTERED_USER)
        login("reg2@example.com", "password12345")
        response = client.get(f"/events/{event.id}")
        assert f"/admin/events/{event.id}" not in response.text

    def test_admin_block_shown_for_administrator(
        self, client, make_city, make_website, make_event, make_user, login
    ):
        _, _, event = _visible_event(make_city, make_website, make_event)
        make_user(email="admin@example.com", password="password12345", role_name=ADMINISTRATOR)
        login("admin@example.com", "password12345")
        response = client.get(f"/events/{event.id}")
        assert f"/admin/events/{event.id}" in response.text
