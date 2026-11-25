# Extraction patterns

All extraction dispatches through the `PatternRegistry` — there are **no**
hostname or site-name conditionals anywhere. A source is entirely described by
a validated `SiteConfiguration` (closed, plain data; `extra="forbid"`; no
executable content). The pipeline is: fetch → detect → extract → normalize →
validate → dedup, and it is deterministic (identical input + config → identical
output).

## Registered patterns (17)

| Pattern | Source shape |
|---------|--------------|
| `wordpress_rest` | WordPress REST events API |
| `json_ld_event` | schema.org `Event` JSON-LD (`eventStatus: EventCancelled` marks the event cancelled: it leaves the listing, is never deleted, and saved-event alerts go out) |
| `generic_html_cards` | inferred repeated HTML cards (higher approval bar) |
| `the_events_calendar` | The Events Calendar (Tribe) REST |
| `livewhale_json` | LiveWhale JSON feed |
| `simpleview_events` | Simpleview DMO event API (`docs.docs` records) |
| `ics_calendar` | iCalendar/ICS feed (UID identity; RRULE preserved) |
| `rss_atom_events` | RSS/Atom (never uses pubDate as the event date; venue from `ev:location`, `event:location`, `<venue>` or `<location>`, coordinates from `event:coordinates`, `georss:point` or `geo:lat`/`geo:long`) |
| `embedded_json` | JSON embedded in a `<script>` |
| `next_data` | Next.js `__NEXT_DATA__` |
| `nuxt_payload` | Nuxt `__NUXT_DATA__` |
| `algolia_search` | Algolia search response (key via secret reference) |
| `campus_labs_engage` | Campus Labs Engage discovery API (`value` records; recipe with `endsAfter`/`skip`/`take`) |
| `paged_json_events` | Paged JSON API (`content` + page metadata, Spring-style); fields inferred |
| `bibliocommons_events` | BiblioCommons library events gateway (`events.items` ids looked up in `entities`) |
| `audienceview_events` | AudienceView box-office listing (`searchHeaders` / `searchResults` table in a script) |
| `inline_json_events` | JSON event list assigned to a JS variable (`window.x = [...]`) |

## Dates, times and timezones

Every stored event date and time is the event's **local wall clock**, and
`Event.timezone` names the IANA zone it is in. The rules:

- The zone is the source's `SiteConfiguration.timezone`, else the website's
  `timezone_override`, else its town's `City.timezone`
  (`app.core.timezones.resolve_zone_name`). A bare US abbreviation such as
  `EST` gives way to a regional fallback, or else is read as the region it
  stands for (`America/New_York`), so daylight saving time always applies.
- A value that carries its own offset (`...Z`, `...-05:00`, an aware datetime,
  an ICS `TZID` or UTC time, a unix timestamp a pattern emits as `+00:00`) is
  converted to that zone in `normalize_candidate` before the date and time are
  split. Values without an offset, and date-only values, are taken as already
  local. A date-only value is a calendar date, never a UTC midnight.
- A town still on the UTC default has no real zone, so an offset-carrying
  value keeps the source's own clock there.
- "Today" on the public site is each town's own date
  (`app.repositories.public_events.town_today` and `local_today`), never UTC's.

## Shared capabilities (Phase 8G)

- **Date ranges** (`app/extraction/date_ranges.py`): a closed parser for
  explicit multi-day forms (shared-month/year, cross-month, fully explicit,
  ISO, weekday-prefixed). It never invents a missing year/month/end or accepts
  a reversed range — ambiguous values are rejected with provenance.
- **Recurrence** (`app/schemas/recurrence.py`, `app/extraction/recurrence.py`):
  a validated spec (`parent_only` / `explicit_occurrences` / `bounded_expand`)
  and a `dateutil`-backed expander, bounded on every axis (horizon, per-parent
  cap, per-run budget, rule length, sub-daily refusal, execution time).
  Deterministic occurrence identity; wall-clock (DST-correct) times; cancelled
  occurrences flagged and hidden, never deleted.
- **Geography** (`app/services/geographic_filter.py`): post-normalization,
  pre-persistence filtering over locality/region/country/postal/address/radius/
  bounding-box/aliases with any/all mode. No fuzzy matching, no geocoding;
  inclusion is decided from the event's own geography, never its assigned city.
  Missing-geography policy: reject / keep_with_warning / needs_review.
- **Descriptions and venues** (`app/extraction/event_page_fallback.py`): a
  source's own `summary`/`excerpt` stands in for a missing `description`. On
  approved runs (not previews), an event still without a description, or
  without a venue (or with a placeholder such as "TBA"), gets it from its own
  page with one fetch: the JSON-LD Event `description` (else `og:`/`twitter:`/
  `<meta>` description) and the JSON-LD Event `location` (a Place, a string or a
  list; a VirtualLocation is not a venue), else the page's single microdata
  `itemprop="location"`. A missing address is filled from the same place. At
  most 30 pages per run, skipping events already stored with the field (an
  administrator's venue correction counts); meta text repeated across pages or
  matching the listing's own is treated as site boilerplate and dropped.
  `scripts/description_coverage.py [--field venue]` reports which sources still
  lack descriptions or venues.
  The same lookup reads a LiveWhale page's own detail block
  (`#lw_cal_event_detail_meta`) for the venue when the JSON-LD names only an
  online stream, and fills a missing time: an event listed at a bare 12:00 AM
  with no offset (or date-only, when its page is fetched anyway) takes the
  JSON-LD `startDate`/`endDate` (else `og:start_time`), only when that falls on
  the listing's own day in the town's zone. All-day events are never looked up.
  Eventbrite listings give only a date while every Eventbrite event page states
  its time, so a date-only Eventbrite event is always looked up for its time
  (until it is stored with one). Eventbrite is recognised by the page's
  domain (`_PAGE_TIMED_HOSTS` in `event_page_fallback.py`): a deliberate
  exception to keeping site behaviour in `SiteConfiguration`, since the rule
  holds for every Eventbrite source and a per-source setting would need a
  configuration change on each one. A date-only end is replaced only by a page end
  on that same day. A page with no usable time leaves the event "Time TBD".
  Before any page is fetched, the run asks the same geographic filter and
  home-town check it applies afterwards: an event it will leave out costs no
  fetch and no budget (`event_page_lookup_skipped_out_of_town:N`).
  A page read successfully that names no venue is remembered for 7 days
  (`event_page_checks`, recorded by imports only) and not fetched again for a
  venue in that time, so the 30-page budget moves on to pages not yet read
  instead of re-reading the same first pages every day
  (`venue_lookup_recently_checked:N_events_skipped`). Failed and blocked
  fetches are never remembered and are counted apart from pages that simply
  name no venue (`event_page_lookup_fetch_failed:N_pages`,
  `event_page_lookup_fetch_blocked:N_pages`,
  `venue_lookup_page_names_no_venue:N_pages`). JSON-LD is parsed leniently, so
  a raw line break inside a string doesn't hide the whole block.
- **Detail selectors** (`app/extraction/detail_pages.py`): `detail_<field>`
  selectors read the card's `detail_link`, or the event's `canonical_url` when
  the record has no detail link (an RSS or JSON feed), within
  `max_detail_fetches`.
- **Locations** (`app/extraction/locations.py`): reads a schema.org `location`
  of any shape. The `json_ld_event` pattern uses it when `location.name` finds
  nothing, and `livewhale_json` falls back to `location_title`, `location_name`,
  `place_title`, `place` or a nested place object when the free-text `location`
  is empty or a placeholder ("TBD", "Location TBA").
- **Home venue** (`SiteConfiguration.home_venue_name` / `home_venue_address`,
  "Home venue" on the configure page): for an organizer that holds every event
  at its own venue and never names it per event (a single theater or museum).
  An administrator types the name (and optionally the address); it is never set
  automatically. It fills only an event with no venue of its own, after the
  listing, any detail selectors and the page lookup have run; a placeholder
  ("TBA") counts as missing. Its address is used only with it. Provenance
  records the venue as `source:home_venue` and the history carries
  `venue:source_home_venue`. A re-import never replaces a stored venue with the
  home venue, and an administrator's location correction stays the public
  venue. Like any configuration change it is a draft until approved.
- **LiveWhale times**: `date_iso`/`date2_iso` (the event's local time with its
  offset) are always preferred, even when a saved json_path names `date_ts`; a
  timestamp that names a different instant is reported as
  `livewhale_time_mismatch` and not used. Start and end are read independently,
  and an empty `date2_iso` means "no end", which clears a stale stored end.
- **UTC fields**: a naive date-time read from a path ending in `_gmt` or `_utc`
  (WordPress `date_gmt`, LiveWhale `date_utc`) is read as UTC and moved to the
  town's clock.
- **Recheck** (`app/services/event_recheck.py`, Advanced tools on a LiveWhale
  website's admin page): reads current and upcoming events that show 12:00 AM,
  have no venue, or were not refreshed by the latest import back from the
  source by their own id, previews the corrections, and applies them in place
  only when asked. Rows it can't match to exactly one record and session are
  listed for review, never changed.

## Fetch strategies

- **HTTP** (`HttpFetchStrategy`) — default; SSRF-validated; supports secret
  header references (`env:NAME`) resolved at request time and never stored.
- **Browser** (`BrowserFetchStrategy`, Phase 9) — restricted Playwright for
  JS-rendered sources; a closed action plan (no arbitrary JS), SSRF-revalidated
  subrequests, challenge walls reported-not-solved, always torn down. Prefer a
  discovered structured API over rendered HTML.

## Structured-source scoring (browser recovery)

`app/extraction/structured_candidates.py` ranks browser-observed responses as
event sources by *evidence*, not by whether a detector already knows the shape:
same-registrable-domain ownership, an event-record array with event-like fields,
and structural signals, minus penalties for telemetry/aggregate/empty shapes.
This lets recovery prefer a first-party event API in an unknown format over a
zero-result rendered-HTML proposal, and surface "a reusable pattern is needed"
instead of forcing `generic_html_cards` onto JSON.

**`bibliocommons_events`.** Public libraries on BiblioCommons build their
events page in the browser from a public gateway API
(`gateway.bibliocommons.com/v2/libraries/<library>/events`). A page of that API
lists event ids in `events.items`; each event, its branch (`entities.locations`)
or other place (`entities.places`), its type and its image are looked up by id
in `entities`. Start and end are already local wall-clock times. The detector
matches that response shape, or a library page on `<library>.bibliocommons.com`
that mentions the platform in its markup, and the proposer stores the gateway
URL with `limit=100` and walks it with `path_page` pagination
(`...&page={page}`). Event links follow the platform's `/events/<id>` route.
Cancelled events are skipped.

**`audienceview_events`.** Box offices on AudienceView Unlimited (for example
a performing-arts center whose `/upcoming-events` redirects to
`/Online/default.asp?...permalink=upcoming-events`) server-render the whole
listing into a script as `articleContext = { searchHeaders : [...],
searchResults : [[...], ...] }` and draw the cards with JavaScript. A plain HTTP
fetch already holds that table, so no browser is needed. The pattern finds both
literals textually and parses them with `json.loads` (only the JS `\'` escape is
rewritten first; nothing is executed). Each row is keyed by header name.
Title is `Short Description` (else `Data 1`); start comes from the display
column ("Thursday, December 3, 2026 / 7:00 PM"), falling back to the split
`Start Date year/month/date/time` columns, whose month is zero-based. The event
URL is the "MORE INFO" link in `Description`; venue is `Venue Description`,
address `Venue Data 10`, description `Meta Description` or the `Data 1` /
`Data 2` subtitle lines, category `filter1`. The performance `Id` is the dedup
identity, so a 7 PM and a 9 PM set of the same show stay separate events.
Rows whose `Object Type` is not `P` (packages, bundles) are skipped. Defaults
point at a derived `event.*` object; `json_paths` can point any field at a raw
column instead (`"description": "Data 2"`).

**`simpleview_events`.** Simpleview DMO sites expose a first-party event API
(e.g. `…/includes/rest_v2/plugins_events_events_by_date/find/`) returning a
nested `docs.docs` record array. The detector matches on that structure — a
record array whose objects carry a stable id (`recid`→`_id`→`id`), an event
date (`startDate`/`date`/`endDate`), and a title — never on a URL, the word
"events", or a footer. `recid` is the dedup identity; the extractor maps
`config.json_paths` (root at `json_paths["events_root"]`, default `docs.docs`),
resolves relative detail URLs against the origin, ignores unsafe media URLs, and
preserves recurrence verbatim (never expanded). The aggregate/facet endpoint is
never treated as an event source. Request replay is single-response by default
(pagination left to configure once confirmed); a browser-observed POST body is
replayed only through the closed `FetchConfig` schema, and an observed query
token is never persisted. No hostname/domain/institution literal appears in the
pattern — it is generic across Simpleview deployments.
An empty `events_root` (`""`) reads a response that is itself the record
array (`[{...}, ...]`), which lets the same mapping serve other event APIs of
that shape, such as library calendars that answer with a bare list. Pair it with
a `request_recipe` when the API needs a date window in its query.

**`paged_json_events`.** First-party event APIs shaped like a Spring Data
page: records in a top-level `content` array (or `items`/`results`/`records`/
`data` beside page metadata such as `totalPages`, `number`, `last`). Record key
names vary, so each field's path is inferred from a closed list of common names
(one object level deep, or through the first element of an instance list such
as `instances[0].start`, in which case each instance becomes its own event) and
stored in `json_paths` for the admin to override. Epoch, Jackson date-array and
offset-bearing ISO timestamps become local wall-clock times. Pagination is the
`page_metadata` strategy, which follows the response's own page number and stops
on `last`, `totalPages` or an empty page. When records carry an id but no link,
identity is the id and `canonical_url` is not required.

## Advertised calendar feeds

When a listing page matches no pattern, or only `generic_html_cards`,
detection looks for a calendar feed the page itself links (`<link
rel="alternate">` iCalendar/RSS/Atom, `webcal:` and `.ics` anchors; iCalendar
first; never a single-event "add to calendar" download) and, if a feed pattern
recognises it with at least two entries, uses the feed as the source. Browser
recovery does the same before falling back to rendered HTML.

## Adding a pattern

Add a detector + extractor to `app/extraction/patterns/`, register it in the
`PatternRegistry`, and add a proposer under `app/extraction/inference/`. Never
branch on a site's identity; express everything through configuration.

## Determinism & safety

No `eval`/`exec`; transformations are a closed `Literal` kind + plain params.
Regexes are validated for safety. AI never participates in authoritative
extraction — it can only *suggest* (see `docs/architecture.md`).
