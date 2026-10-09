# Extraction patterns

All extraction dispatches through the `PatternRegistry` — there are **no**
hostname or site-name conditionals anywhere. A source is entirely described by
a validated `SiteConfiguration` (closed, plain data; `extra="forbid"`; no
executable content). The pipeline is: fetch → detect → extract → normalize →
validate → dedup, and it is deterministic (identical input + config → identical
output).

## Registered patterns (12)

| Pattern | Source shape |
|---------|--------------|
| `wordpress_rest` | WordPress REST events API |
| `json_ld_event` | schema.org `Event` JSON-LD |
| `generic_html_cards` | inferred repeated HTML cards (higher approval bar) |
| `the_events_calendar` | The Events Calendar (Tribe) REST |
| `livewhale_json` | LiveWhale JSON feed |
| `simpleview_events` | Simpleview DMO event API (`docs.docs` records) |
| `ics_calendar` | iCalendar/ICS feed (UID identity; RRULE preserved) |
| `rss_atom_events` | RSS/Atom (never uses pubDate as the event date) |
| `embedded_json` | JSON embedded in a `<script>` |
| `next_data` | Next.js `__NEXT_DATA__` |
| `nuxt_payload` | Nuxt `__NUXT_DATA__` |
| `algolia_search` | Algolia search response (key via secret reference) |

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

## Adding a pattern

Add a detector + extractor to `app/extraction/patterns/`, register it in the
`PatternRegistry`, and add a proposer under `app/extraction/inference/`. Never
branch on a site's identity; express everything through configuration.

## Determinism & safety

No `eval`/`exec`; transformations are a closed `Literal` kind + plain params.
Regexes are validated for safety. AI never participates in authoritative
extraction — it can only *suggest* (see `docs/architecture.md`).
