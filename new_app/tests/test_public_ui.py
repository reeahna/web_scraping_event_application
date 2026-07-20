from datetime import UTC, datetime, time, timedelta

TODAY = datetime.now(UTC).date()
TOMORROW = TODAY + timedelta(days=1)


def _visible_event(make_city, make_website, make_event, **event_overrides):
    city = make_city()
    website = make_website(city, is_active=True, approved_pattern={"pattern_name": "x"})
    event = make_event(
        city,
        website=website,
        start_date=event_overrides.pop("start_date", TOMORROW),
        **event_overrides,
    )
    return city, website, event


class TestStylesheetLoading:
    def test_stylesheet_link_present_on_homepage(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert '<link rel="stylesheet" href="/static/style.css" />' in resp.text

    def test_stylesheet_returns_200_with_css_content_type(self, client):
        resp = client.get("/static/style.css")
        assert resp.status_code == 200
        assert "text/css" in resp.headers["content-type"]


class TestHumanReadableDateTime:
    def test_homepage_card_shows_human_readable_date_and_time_not_raw_values(
        self, client, make_city, make_website, make_event
    ):
        _, _, event = _visible_event(
            make_city,
            make_website,
            make_event,
            title="Readable Date Event",
            start_date=TOMORROW,
            start_time=time(18, 0),
        )
        resp = client.get("/")
        assert resp.status_code == 200
        assert "Readable Date Event" in resp.text
        assert event.start_date.isoformat() not in resp.text
        assert "18:00:00" not in resp.text
        assert "6:00 PM" in resp.text

    def test_detail_page_shows_human_readable_date_and_time_not_raw_values(
        self, client, make_city, make_website, make_event
    ):
        _, _, event = _visible_event(
            make_city,
            make_website,
            make_event,
            title="Readable Detail Event",
            start_date=TOMORROW,
            start_time=time(9, 30),
        )
        resp = client.get(f"/events/{event.id}")
        assert resp.status_code == 200
        assert event.start_date.isoformat() not in resp.text
        assert "09:30:00" not in resp.text
        assert "9:30 AM" in resp.text


class TestImageFallback:
    def test_event_with_image_renders_img_tag(self, client, make_city, make_website, make_event):
        _, _, event = _visible_event(
            make_city,
            make_website,
            make_event,
            title="Image Event",
            image_url="https://example.com/poster.jpg",
        )
        resp = client.get("/")
        assert f'<img src="{event.image_url}" alt="Image Event"' in resp.text
        assert "event-card-image-fallback" not in resp.text.split("Image Event")[0][-600:]

    def test_event_without_image_renders_fallback(
        self, client, make_city, make_website, make_event
    ):
        _, _, event = _visible_event(
            make_city, make_website, make_event, title="No Image Event", image_url=None
        )
        resp = client.get("/")
        assert "No Image Event" in resp.text
        assert "event-card-image-fallback" in resp.text
        assert "<svg" in resp.text


class TestFilterValuesPreserved:
    def test_selected_filters_round_trip_into_form_controls(
        self, client, make_city, make_website, make_event, make_category
    ):
        city, website, event = _visible_event(make_city, make_website, make_event)
        category = make_category(name="Round Trip Category", slug="round-trip-category")
        make_event(city, website=website, category=category, start_date=TOMORROW)

        resp = client.get(
            f"/?city_id={city.id}&category_id={category.id}"
            f"&date_from={TOMORROW.isoformat()}&date_to={TOMORROW.isoformat()}&upcoming_only=1"
        )
        assert resp.status_code == 200
        assert f'value="{city.id}" selected' in resp.text
        assert f'value="{category.id}" selected' in resp.text
        assert f'value="{TOMORROW.isoformat()}"' in resp.text
        assert 'name="upcoming_only" value="1" checked' in resp.text


class TestEmptyState:
    def test_empty_state_shown_when_no_events_match(self, client, make_city):
        city = make_city(name="Empty City", slug="empty-city")
        resp = client.get(f"/?city_id={city.id}")
        assert resp.status_code == 200
        assert "No events match these filters" in resp.text
        assert 'href="/" class="filter-clear-link"' in resp.text

    def test_events_present_hides_empty_state(self, client, make_city, make_website, make_event):
        _visible_event(make_city, make_website, make_event, title="Present Event")
        resp = client.get("/")
        assert "No events match these filters" not in resp.text
