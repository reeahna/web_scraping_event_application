"""Hiding categories and sources: the inverse of the category/source filters."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

TOMORROW = datetime.now(UTC).date() + timedelta(days=1)


def _site(make_website, city, name):
    return make_website(
        city, name=name, is_active=True, approved_pattern={"pattern_name": "static_html"},
        source_display_name=name,
    )


def _titles(resp) -> set[str]:
    return {t for t in ("Alpha Show", "Beta Show", "Gamma Show") if t in resp.text}


def test_excluding_a_source_hides_only_its_events(client, make_city, make_website, make_event):
    city = make_city()
    alpha, beta = _site(make_website, city, "Alpha"), _site(make_website, city, "Beta")
    make_event(city, website=alpha, title="Alpha Show", start_date=TOMORROW)
    make_event(city, website=beta, title="Beta Show", start_date=TOMORROW,
               canonical_url="https://x/b")

    resp = client.get(f"/city/{city.slug}?exclude_source={alpha.id}")
    assert _titles(resp) == {"Beta Show"}
    # The box stays ticked so the choice survives the next Filter submit.
    assert f'name="exclude_source" value="{alpha.id}" checked' in resp.text


def test_excluding_several_categories(
    client, make_city, make_website, make_event, make_category
):
    city = make_city()
    site = _site(make_website, city, "Alpha")
    music = make_category(name="Music X", slug="music-x")
    talks = make_category(name="Talks X", slug="talks-x")
    make_event(city, website=site, title="Alpha Show", start_date=TOMORROW, category=music)
    make_event(city, website=site, title="Beta Show", start_date=TOMORROW, category=talks,
               canonical_url="https://x/b")
    make_event(city, website=site, title="Gamma Show", start_date=TOMORROW,
               canonical_url="https://x/g")  # uncategorised: never hidden by this

    resp = client.get(
        f"/city/{city.slug}?exclude_category={music.id}&exclude_category={talks.id}"
    )
    assert _titles(resp) == {"Gamma Show"}
    assert "2 hidden" in resp.text


def test_category_exclusion_follows_the_admin_override(
    client, make_city, make_website, make_event, make_category
):
    city = make_city()
    site = _site(make_website, city, "Alpha")
    scraped = make_category(name="Scraped X", slug="scraped-x")
    corrected = make_category(name="Corrected X", slug="corrected-x")
    make_event(city, website=site, title="Alpha Show", start_date=TOMORROW,
               category=scraped, category_override_id=corrected.id)

    # Shown under its override, so hiding the scraped category keeps it...
    assert _titles(client.get(f"/city/{city.slug}?exclude_category={scraped.id}")) == {
        "Alpha Show"
    }
    # ...and hiding the override removes it.
    assert _titles(client.get(f"/city/{city.slug}?exclude_category={corrected.id}")) == set()


def test_exclusions_carry_into_pagination_and_map(client, make_city, make_website, make_event):
    city = make_city()
    alpha, beta = _site(make_website, city, "Alpha"), _site(make_website, city, "Beta")
    make_event(city, website=alpha, title="Alpha Show", start_date=TOMORROW,
               latitude=40.0, longitude=-86.0)
    make_event(city, website=beta, title="Beta Show", start_date=TOMORROW,
               canonical_url="https://x/b", latitude=40.1, longitude=-86.1)

    resp = client.get(f"/city/{city.slug}?exclude_source={alpha.id}&view=map")
    assert f"exclude_source={alpha.id}" in resp.text  # in the map data URL

    points = client.get(f"/events/map?city_id={city.id}&exclude_source={alpha.id}").json()
    assert [p["title"] for p in points["points"]] == ["Beta Show"]


def test_junk_exclusion_values_are_ignored(client, make_city, make_website, make_event):
    city = make_city()
    site = _site(make_website, city, "Alpha")
    make_event(city, website=site, title="Alpha Show", start_date=TOMORROW)
    resp = client.get(f"/city/{city.slug}?exclude_source=abc&exclude_category=")
    assert resp.status_code == 200
    assert _titles(resp) == {"Alpha Show"}
