# Search: showing up for "events near me" and "events in <town>"

What the site does for search engines, and what only the owner can do. Read this
before changing town pages, event pages, the sitemap or robots.txt.

## How these searches work

Nobody searches for "Bulletin". People search for:

- **"things to do in Ann Arbor"**, **"events in Ann Arbor"**, **"Ann Arbor events this
  weekend"**, **"what's on in Ann Arbor today"**
- **"events near me"**, **"things to do this weekend near me"**

Google answers a "near me" search by working out where the searcher is (from their
device's location or IP address) and then ranking pages about that place, so "events
near me" from a phone in Ann Arbor is treated much like "events in Ann Arbor". There
is no "near me" keyword to add to a page. The way to rank for it is to have the best
page for each town, and to make it obvious which town each page is about.

## What the code does

| Page | Answers | Built in |
|---|---|---|
| `/city/<slug>` | "events in Ann Arbor", "things to do in Ann Arbor" | `routers/home.py` `_town_page` |
| `/city/<slug>/this-weekend` | "things to do in Ann Arbor this weekend" | same |
| `/city/<slug>/today` | "things to do in Ann Arbor today" | same |
| `/events/<id>` | the event's own name, plus town and date | `routers/public_events.py` |

For each of those pages:

- **Title, heading and description** name the town, its state and its schools, worded
  the way people search (`seo.town_page_title`, `town_heading`,
  `town_meta_description`). A one-sentence summary under the heading ("142 upcoming
  events in Ann Arbor, Michigan, gathered from 12 local sources, with 18 this
  weekend") gives search engines plain text to match.
- **Structured data (JSON-LD):** town pages carry an `ItemList` of `Event`s whose
  `about` is the `City` with its state and coordinates, and a `BreadcrumbList`
  (Towns > Ann Arbor > This weekend). Event pages carry an `Event` with its `Place`
  and coordinates, and a `BreadcrumbList`. Google uses the breadcrumb in place of the
  bare URL in results, and Event data can make an event eligible for richer results.
- **Location signals:** the town's coordinates in the `City` data, each event's venue
  coordinates, and links between neighbouring towns ("Events near Ann Arbor", any
  active town within 250 miles). A town's position is its default latitude and
  longitude from Admin > Cities; when those are empty it is the median of its own
  public events' coordinates (`seo.town_centers`, at least 3 events, cached for half
  an hour). States are always spelled out ("Ann Arbor, Michigan"), whether the town
  was entered as "MI" or "Michigan" (`app/core/us_states.py`).
- **Where the listings come from:** each town page names and links the sites its
  events are gathered from (the site's root, never a feed or API address). It is
  accurate, different for every town, and credits the local calendars.
- **Event structured data** follows Google's Event rules: every event has a
  `location` with a `PostalAddress` (the venue and its street when the source gives
  them, otherwise the town, which Google's guidelines allow), times carry their UTC
  offset from the event's or town's zone, and nothing is invented: no organizer
  (the only name on record is the source's), price, venue or image the source did
  not give. The event page repeats the times in `<time datetime>` tags.
- **Indexing rules:** a town with fewer than `MIN_INDEXABLE_TOWN_EVENTS` upcoming
  events is `noindex` and left out of the sitemap, and so are its weekend and today
  pages. Filtered views (`?q=`, `?category_id=`, `?preset=`) point their canonical
  link at the plain town page, so they never compete with it. The weekend and today
  pages are real paths for that reason: a query string variant can never rank on
  its own.
- **Sitemap and robots:** `/sitemap.xml` lists every indexable town's three pages and
  every upcoming event (up to 45,000 URLs in one file; past that it needs a sitemap
  index), with `<lastmod>` from when an event's details last changed; `/robots.txt` points to it and keeps crawlers out of `/admin`
  and `/account`. `/llms.txt` describes the site for AI tools.

## What only the owner can do

These matter more than anything in the code, and none can be done from the repository.

1. **Use a custom domain.** A site on `*.onrender.com` shares its address with
   thousands of others, ranks poorly, and loses everything it has built up if it ever
   moves. Buy a short domain, add it in Render (Settings, Custom Domains) and set
   `PUBLIC_BASE_URL` to it, so canonical links and the sitemap name the new address.
   Do this before anything below, because search engines remember addresses.
2. **Verify the site in Google Search Console** (`search.google.com/search-console`),
   as a Domain property, then submit `https://<domain>/sitemap.xml`. While the site is
   still on the Render address (which takes no DNS record), add a URL prefix property,
   choose "HTML tag", and set the `GOOGLE_SITE_VERIFICATION` environment variable to
   the tag's `content` value (`BING_SITE_VERIFICATION` does the same for Bing). This is how
   Google learns about new pages quickly, and the only place that shows which
   searches the site appears for, which pages are indexed, and any structured data
   errors. Do the same in **Bing Webmaster Tools** (it can import from Search Console);
   Bing also feeds ChatGPT search and DuckDuckGo.
3. **Get linked from each town.** Links from local sites are the strongest signal that
   a page is the place to go for a town: student government and student newspaper
   resource pages, university "things to do" or orientation pages, the town's visitor
   bureau, local subreddits' sidebars and wikis. One genuine link from a campus page
   is worth more than any change to titles.
4. **Check results after a few weeks.** In Search Console, under Performance, filter
   queries containing "events" or "things to do" and see which towns get impressions.
   A town with impressions but few clicks needs a better title or description; one with
   none usually needs more events or a local link.

A Google Business Profile does not apply: it is for businesses with a physical place
or service area, and Bulletin is a website.

## Checking a change

- `python -m pytest tests/test_seo.py tests/test_seo_event_structured_data.py tests/test_seo_town_pages.py tests/test_seo_sitemap_and_verification.py tests/test_ai_discovery.py -q`
- Google's Rich Results Test (`search.google.com/test/rich-results`) on a live town
  page and event page shows how Google reads the JSON-LD.
