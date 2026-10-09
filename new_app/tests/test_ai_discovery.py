"""What AI tools, crawlers and calendar apps can read: robots.txt, llms.txt,
the per-town calendar feed, and the structured data on listing pages."""

import json
import re
from datetime import UTC, datetime, time, timedelta

from icalendar import Calendar

from app.routers.seo import AI_CRAWLERS

TOMORROW = datetime.now(UTC).date() + timedelta(days=1)


def _town(make_city, make_website, name="Bloomington", slug="bloomington", **city):
    town = make_city(name=name, slug=slug, timezone="America/Indiana/Indianapolis", **city)
    site = make_website(
        town, name=f"{name} Src", is_active=True,
        approved_pattern={"pattern_name": "static_html"},
    )
    return town, site


def _ld_blocks(html):
    return [
        json.loads(m)
        for m in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)
    ]


# --- robots.txt ------------------------------------------------------------------


def test_ai_crawlers_are_named_and_share_the_private_area_rules(client):
    text = client.get("/robots.txt").text
    for name in ("GPTBot", "ClaudeBot", "Google-Extended", "PerplexityBot"):
        assert f"User-agent: {name}" in text
    # One group: every User-agent line comes before the first rule. A separate
    # group per bot would exempt it from the Disallow lines.
    lines = text.splitlines()
    first_rule = next(i for i, line in enumerate(lines) if line.startswith("Disallow"))
    agents = [i for i, line in enumerate(lines) if line.startswith("User-agent")]
    assert len(agents) == len(AI_CRAWLERS) + 1 and max(agents) < first_rule
    assert "Disallow: /admin/" in text and "Allow" not in text.replace("Disallow", "")


# --- llms.txt --------------------------------------------------------------------


def test_llms_txt_lists_active_towns_and_their_feeds(
    client, make_city, make_website, make_event
):
    town, site = _town(make_city, make_website, universities=["Indiana University"])
    make_event(town, website=site, title="Jazz Night", start_date=TOMORROW)
    make_city(name="Hidden Town", slug="hidden", is_active=False)

    resp = client.get("/llms.txt")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    text = resp.text
    assert text.startswith("# ")
    assert "/city/bloomington/events.ics" in text
    assert "Indiana University" in text and "1 upcoming event;" in text
    assert "Hidden Town" not in text
    assert "/sitemap.xml" in text


# --- calendar feed -----------------------------------------------------------------


def test_town_feed_holds_exactly_the_towns_public_events(
    client, make_city, make_website, make_event
):
    town, site = _town(make_city, make_website)
    other, other_site = _town(make_city, make_website, name="Elsewhere", slug="elsewhere")
    make_event(town, website=site, title="Jazz Night", start_date=TOMORROW,
               start_time=time(19, 30), end_time=time(22, 0), venue="The Bishop")
    make_event(town, website=site, title="Craft Fair", start_date=TOMORROW,
               end_date=TOMORROW + timedelta(days=1), canonical_url="https://x/fair")
    make_event(town, website=site, title="Hidden", start_date=TOMORROW, is_active=False,
               canonical_url="https://x/hidden")
    make_event(other, website=other_site, title="Not Here", start_date=TOMORROW,
               canonical_url="https://x/other")

    resp = client.get("/city/bloomington/events.ics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/calendar")
    cal = Calendar.from_ical(resp.content)
    events = {str(e["summary"]): e for e in cal.walk("VEVENT")}
    assert set(events) == {"Jazz Night", "Craft Fair"}

    jazz = events["Jazz Night"]
    start = jazz.decoded("dtstart")
    assert start.hour == 19 and str(start.tzinfo) == "America/Indiana/Indianapolis"
    assert jazz.decoded("dtend").hour == 22
    assert "The Bishop" in str(jazz["location"])
    assert str(jazz["url"]).endswith(f"/events/{jazz['uid'].split('-')[1].split('@')[0]}")
    assert cal.walk("VTIMEZONE"), "strict calendar apps need the zone definition"
    assert b"X-PUBLISHED-TTL:PT6H" in resp.content

    fair = events["Craft Fair"]
    # All-day: the end date is exclusive, so a two-day fair ends the day after.
    assert fair.decoded("dtend") == TOMORROW + timedelta(days=2)


def test_a_cancelled_event_is_marked_cancelled(client, make_city, make_website, make_event):
    town, site = _town(make_city, make_website)
    make_event(town, website=site, title="Rained Out", start_date=TOMORROW, is_cancelled=True)
    cal = Calendar.from_ical(client.get("/city/bloomington/events.ics").content)
    assert [str(e["status"]) for e in cal.walk("VEVENT")] == ["CANCELLED"]


def test_feed_for_an_unknown_or_hidden_town_is_404(client, make_city):
    make_city(name="Hidden Town", slug="hidden", is_active=False)
    assert client.get("/city/nowhere/events.ics").status_code == 404
    assert client.get("/city/hidden/events.ics").status_code == 404


def test_town_page_links_its_feed(client, make_city, make_website):
    _town(make_city, make_website)
    html = client.get("/city/bloomington").text
    assert 'type="text/calendar"' in html
    assert "/city/bloomington/events.ics" in html
    assert "Subscribe in your calendar" in html


# --- structured data on listing pages --------------------------------------------


def test_town_page_describes_its_events_as_an_item_list(
    client, make_city, make_website, make_event
):
    town, site = _town(make_city, make_website, universities=["Indiana University"])
    make_event(town, website=site, title="Jazz Night", start_date=TOMORROW, venue="The Bishop")
    make_event(town, website=site, title="Craft Fair", start_date=TOMORROW,
               canonical_url="https://x/fair")

    (data,) = _ld_blocks(client.get("/city/bloomington").text)
    assert data["@type"] == "ItemList"
    assert data["about"]["name"] == "Bloomington"
    assert data["keywords"] == ["Indiana University"]
    names = [i["item"]["name"] for i in data["itemListElement"]]
    assert sorted(names) == ["Craft Fair", "Jazz Night"]
    assert all(i["item"]["@type"] == "Event" for i in data["itemListElement"])


def test_a_scraped_title_cannot_break_out_of_the_listing_data(
    client, make_city, make_website, make_event
):
    town, site = _town(make_city, make_website)
    make_event(town, website=site, title="Evil </script><img src=x id=pwned>",
               start_date=TOMORROW)
    html = client.get("/city/bloomington").text
    assert "<img src=x id=pwned>" not in html
    (data,) = _ld_blocks(html)
    assert data["itemListElement"][0]["item"]["name"] == "Evil </script><img src=x id=pwned>"


def test_home_page_describes_the_site_and_its_search(client):
    (data,) = _ld_blocks(client.get("/").text)
    assert data["@type"] == "WebSite"
    assert data["potentialAction"]["target"]["urlTemplate"].endswith("?q={search_term_string}")


def test_terms_welcome_reading_and_indexing(client):
    text = client.get("/terms").text
    assert "welcome to read, link to and index" in text
    assert "scrape" not in text
