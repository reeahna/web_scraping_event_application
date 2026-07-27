"""Phase 12: search, source/recurrence filters, presets, city pages, map data."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

TODAY = datetime.now(UTC).date()
TOMORROW = TODAY + timedelta(days=1)


def _visible_website(make_city, make_website, city=None, name="Src"):
    city = city or make_city()
    website = make_website(
        city, name=name, is_active=True, approved_pattern={"pattern_name": "static_html"},
        source_display_name=name,
    )
    return city, website


def test_search_filters_by_title(client, make_city, make_website, make_event):
    city, website = _visible_website(make_city, make_website)
    make_event(city, website=website, title="Jazz Night", start_date=TOMORROW)
    make_event(city, website=website, title="Poetry Slam", start_date=TOMORROW,
               canonical_url="https://x/p")
    resp = client.get("/?q=jazz")
    assert "Jazz Night" in resp.text
    assert "Poetry Slam" not in resp.text


def test_source_filter(client, make_city, make_website, make_event):
    city = make_city()
    _, site_a = _visible_website(make_city, make_website, city=city, name="Alpha")
    _, site_b = _visible_website(make_city, make_website, city=city, name="Beta")
    make_event(city, website=site_a, title="Alpha Event", start_date=TOMORROW)
    make_event(city, website=site_b, title="Beta Event", start_date=TOMORROW,
               canonical_url="https://x/b")
    resp = client.get(f"/?source_id={site_a.id}")
    assert "Alpha Event" in resp.text
    assert "Beta Event" not in resp.text


def test_recurrence_filter(client, make_city, make_website, make_event):
    city, website = _visible_website(make_city, make_website)
    make_event(city, website=website, title="Weekly Market", start_date=TOMORROW,
               recurrence_parent_id="P1")
    make_event(city, website=website, title="One Off Gala", start_date=TOMORROW,
               canonical_url="https://x/g")
    recurring = client.get("/?recurrence=recurring")
    assert "Weekly Market" in recurring.text
    assert "One Off Gala" not in recurring.text
    single = client.get("/?recurrence=single")
    assert "One Off Gala" in single.text
    assert "Weekly Market" not in single.text


def test_today_preset(client, make_city, make_website, make_event):
    city, website = _visible_website(make_city, make_website)
    make_event(city, website=website, title="Today Show", start_date=TODAY)
    make_event(city, website=website, title="Tomorrow Show", start_date=TOMORROW,
               canonical_url="https://x/t")
    resp = client.get("/?preset=today")
    assert "Today Show" in resp.text
    assert "Tomorrow Show" not in resp.text


def test_recurrence_parent_is_never_shown(client, make_city, make_website, make_event):
    city, website = _visible_website(make_city, make_website)
    make_event(city, website=website, title="Series Parent", start_date=TOMORROW,
               is_recurrence_parent=True)
    resp = client.get("/")
    assert "Series Parent" not in resp.text


def test_city_page_scopes_to_city(client, make_city, make_website, make_event):
    city_a = make_city(name="Alpha City", slug="alpha-city")
    city_b = make_city(name="Beta City", slug="beta-city")
    _, site_a = _visible_website(make_city, make_website, city=city_a, name="A")
    _, site_b = _visible_website(make_city, make_website, city=city_b, name="B")
    make_event(city_a, website=site_a, title="Alpha Only", start_date=TOMORROW)
    make_event(city_b, website=site_b, title="Beta Only", start_date=TOMORROW,
               canonical_url="https://x/b")
    resp = client.get("/city/alpha-city")
    assert resp.status_code == 200
    assert "Alpha Only" in resp.text
    assert "Beta Only" not in resp.text


def test_unknown_city_page_404(client):
    assert client.get("/city/nope").status_code == 404


def test_map_endpoint_returns_only_events_with_coordinates(
    client, make_city, make_website, make_event
):
    city, website = _visible_website(make_city, make_website)
    make_event(city, website=website, title="Located", start_date=TOMORROW,
               latitude=39.8, longitude=-89.6)
    make_event(city, website=website, title="No Coords", start_date=TOMORROW,
               canonical_url="https://x/n")
    resp = client.get("/events/map")
    assert resp.status_code == 200
    data = resp.json()
    titles = {p["title"] for p in data["points"]}
    assert "Located" in titles
    assert "No Coords" not in titles


def test_map_payload_carries_nothing_sensitive(client, make_city, make_website, make_event):
    city, website = _visible_website(make_city, make_website)
    make_event(city, website=website, title="Located", start_date=TOMORROW,
               latitude=39.8, longitude=-89.6)
    point = client.get("/events/map").json()["points"][0]
    assert set(point.keys()) == {
        "id", "title", "url", "latitude", "longitude", "start_date", "venue", "category"
    }


def test_map_prefers_geocoded_when_no_source_coords(
    client, make_city, make_website, make_event
):
    city, website = _visible_website(make_city, make_website)
    make_event(city, website=website, title="Geo", start_date=TOMORROW,
               geocoded_latitude=40.0, geocoded_longitude=-88.0)
    point = client.get("/events/map").json()["points"][0]
    assert point["latitude"] == 40.0


def test_map_view_renders_container(client, make_city, make_website, make_event):
    city, website = _visible_website(make_city, make_website)
    make_event(city, website=website, title="X", start_date=TOMORROW,
               latitude=39.8, longitude=-89.6)
    resp = client.get("/?view=map")
    assert 'id="event-map"' in resp.text
    assert "leaflet" in resp.text.lower()
