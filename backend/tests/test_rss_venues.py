"""Venues and coordinates from RSS event feeds.

Each feed below mirrors the shape of a real source: the RSS event module's
`ev:location` (an arts center, a student union's RDF feed), a parks
department's `event:location` with `event:coordinates`, and a plain `<venue>`
element with an occasional placeholder. Only the feed's own location element
is read; nothing is inferred from the title or description.
"""

from __future__ import annotations

from app.extraction.normalize import normalize_candidate
from app.extraction.patterns.rss_atom_events import RssAtomEventsPattern
from app.schemas.extraction import SiteConfiguration
from tests.extraction_helpers import make_response

URL = "https://venue.example.org/events/rss"


def _extract(feed: str, **json_paths: str):
    config = SiteConfiguration(
        pattern_name="rss_atom_events",
        listing_url=URL,
        json_paths={"start_datetime": "startdate", **json_paths},
        required_fields=["title", "start_date"],
    )
    candidates = RssAtomEventsPattern().extract(
        make_response(feed, final_url=URL, content_type="text/xml"), config
    )
    return candidates, [normalize_candidate(c, config) for c in candidates]


EV_MODULE_RSS = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:ev="http://purl.org/rss/1.0/modules/event/">
  <channel>
    <title>Arts Center</title>
    <item>
      <title>Disney&apos;s The Lion King</title>
      <link>https://venue.example.org/events/lion-king</link>
      <guid>lion-king</guid>
      <description>Giraffes strut at The Kentucky Center.</description>
      <ev:location>Whitney Hall</ev:location>
      <ev:startdate>2026-11-24T19:30:00-05:00</ev:startdate>
    </item>
    <item>
      <title>Comedy Night</title>
      <link>https://venue.example.org/events/comedy</link>
      <guid>comedy</guid>
      <ev:location>Old Forester&apos;s Paristown Hall</ev:location>
      <ev:startdate>2026-11-25T20:00:00-05:00</ev:startdate>
    </item>
  </channel>
</rss>
"""


def test_rss_event_module_location_is_the_venue():
    candidates, events = _extract(EV_MODULE_RSS)
    assert [e.venue for e in events] == ["Whitney Hall", "Old Forester's Paristown Hall"]
    assert candidates[0].field_source_paths["venue"] == "item[0].location|venue"


UNION_RDF = """<?xml version="1.0" encoding="utf-8"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
  xmlns:ev="http://purl.org/rss/1.0/modules/event/" xmlns="http://purl.org/rss/1.0/">
  <item rdf:about="https://union.example.edu/events/coffee">
    <title>Coffee Tent</title>
    <link>https://union.example.edu/events/coffee</link>
    <ev:location>Main Oval</ev:location>
    <ev:startdate>2026-11-27T09:00:00-05:00</ev:startdate>
    <guid>coffee</guid>
  </item>
</rdf:RDF>
"""


def test_rss1_rdf_feed_location_is_the_venue():
    _, events = _extract(UNION_RDF)
    assert events[0].venue == "Main Oval"


PARKS_RSS = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:event="http://www.example.gov/xml/event">
  <channel>
    <item>
      <title><![CDATA[Forest Fitness]]></title>
      <guid>2218042</guid>
      <link>http://www.example.gov/events/2026/11/24/forest-fitness</link>
      <description><![CDATA[<p>Meet at the Heather Garden entrance.</p>]]></description>
      <event:parknames>Fort Tryon Park</event:parknames>
      <event:startdate>2026-11-24</event:startdate>
      <event:location><![CDATA[Margaret Corbin Circle (in Fort Tryon Park)]]></event:location>
      <event:coordinates>40.85941194491500000, -73.93413484096500000</event:coordinates>
    </item>
    <item>
      <title><![CDATA[Bad Coordinates]]></title>
      <guid>2</guid>
      <link>http://www.example.gov/events/2026/11/24/bad</link>
      <event:startdate>2026-11-24</event:startdate>
      <event:location><![CDATA[Bryant Park]]></event:location>
      <event:coordinates>140.1, -73.9</event:coordinates>
    </item>
  </channel>
</rss>
"""


def test_parks_feed_location_and_coordinates():
    candidates, events = _extract(PARKS_RSS)
    first = events[0]
    assert first.venue == "Margaret Corbin Circle (in Fort Tryon Park)"
    assert first.latitude == 40.859411944915
    assert first.longitude == -73.934134840965
    assert candidates[0].field_source_paths["latitude"] == "item[0].coordinates|point"


def test_out_of_range_coordinates_are_ignored_but_the_venue_is_kept():
    _, events = _extract(PARKS_RSS)
    assert events[1].venue == "Bryant Park"
    assert events[1].latitude is None and events[1].longitude is None


VENUE_RSS = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0">
  <channel>
    <item>
      <title>Fatherland</title>
      <link>https://trust.example.org/production/1/fatherland</link>
      <image>
        <url>https://trust.example.org/hero.jpg</url>
        <title>Fatherland</title>
        <link>https://trust.example.org/production/1/fatherland</link>
      </image>
      <datetime>Tue, Nov 24, 2026, 7:30PM</datetime>
      <venue>Harris Theater</venue>
    </item>
    <item>
      <title>Poinsettias &amp; Pointe Shoes</title>
      <link>https://trust.example.org/production/2/poinsettias</link>
      <datetime>Tue, Nov 24, 2026, 6:45PM</datetime>
      <venue>See Event Description</venue>
    </item>
    <item>
      <title>Gallery Crawl</title>
      <link>https://trust.example.org/production/3/crawl</link>
      <datetime>Fri, Dec 4, 2026, 5:30PM</datetime>
      <venue/>
    </item>
    <item>
      <title>Holiday Show</title>
      <link>https://trust.example.org/production/4/holiday</link>
      <datetime>Sat, Dec 5, 2026, 2:00PM</datetime>
      <venue>TBA</venue>
    </item>
  </channel>
</rss>
"""


def test_plain_venue_element_is_read_and_placeholders_are_dropped():
    candidates, events = _extract(VENUE_RSS, start_datetime="datetime")
    assert [e.venue for e in events] == ["Harris Theater", None, None, None]
    assert "venue" not in candidates[1].field_source_paths


def test_a_feed_without_a_location_element_still_has_no_venue():
    feed = """<?xml version="1.0"?><rss version="2.0"><channel><item>
      <title>Talk</title><link>https://x.example.org/talk</link><guid>t</guid>
      <description>Held at Memorial Hall, room 2.</description>
      <startdate>2026-12-01T18:00:00-05:00</startdate>
    </item></channel></rss>"""
    candidates, events = _extract(feed)
    # The description names a hall, but a venue is never read from free text.
    assert events[0].venue is None
    assert candidates[0].raw["venue"] is None


def test_json_paths_can_name_the_venue_and_address_elements():
    feed = """<?xml version="1.0"?><rss version="2.0" xmlns:x="urn:x"><channel><item>
      <title>Reading</title><link>https://x.example.org/r</link><guid>r</guid>
      <location>Ignored default</location>
      <x:place>Main Library</x:place><x:street>343 S Fifth Ave</x:street>
      <startdate>2026-12-01T18:00:00-05:00</startdate>
    </item></channel></rss>"""
    candidates, events = _extract(feed, venue="place", address="street")
    assert events[0].venue == "Main Library"
    assert events[0].address == "343 S Fifth Ave"
    assert candidates[0].field_source_paths["address"] == "item[0].street"


def test_w3c_geo_lat_long_are_read():
    feed = """<?xml version="1.0"?><rss version="2.0"
      xmlns:geo="http://www.w3.org/2003/01/geo/wgs84_pos#"><channel><item>
      <title>Walk</title><link>https://x.example.org/w</link><guid>w</guid>
      <geo:lat>42.28</geo:lat><geo:long>-83.74</geo:long>
      <startdate>2026-12-01T18:00:00-05:00</startdate>
    </item></channel></rss>"""
    _, events = _extract(feed)
    assert (events[0].latitude, events[0].longitude) == (42.28, -83.74)


def test_a_venue_that_repeats_the_title_is_not_a_venue():
    feed = """<?xml version="1.0"?><rss version="2.0"><channel><item>
      <title>Winter Market</title><link>https://x.example.org/m</link><guid>m</guid>
      <venue>Winter Market</venue>
      <startdate>2026-12-01T18:00:00-05:00</startdate>
    </item></channel></rss>"""
    _, events = _extract(feed)
    assert events[0].venue is None
