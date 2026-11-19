# Changelog

Every release of Bulletin, newest first. The app reads this file to build two pages:

- **What's new** (`/whats-new`), for everyone: only the **For everyone** sections.
- **Version history** (`/admin/version-history`), for admins: everything,
  including **Admin and behind the scenes** and anything still **Unreleased**.

Version numbers follow [semantic versioning](https://semver.org): the first number
goes up for a big change in how the site works, the second for new features, the
third for fixes only. The newest released version below is the app's version and
shows in the site footer.

## How to keep it current

1. In every pull request, add a line under **Unreleased**: under **For everyone** if
   a visitor or signed-in user would notice it, under **Admin and behind the scenes**
   if only admins or the scraping pipeline would. Write it for that reader, and end
   it with the PR number, like `(#67)`. The public page hides PR numbers.
2. To cut a release, run `python scripts/release.py minor` from `backend/` (or
   `patch` for fixes only, `major` for a big change). It turns **Unreleased** into
   the next version with today's date and bumps `pyproject.toml`. Commit that.
   You rarely need to: every night at 10:30 PM Indianapolis time (11:30 PM in
   summer) a GitHub Actions job (`.github/workflows/nightly-release.yml`) runs
   `release.py auto`, which cuts a minor release when there are **For everyone**
   notes and a patch release when there are only admin notes, and commits it to
   `main`. When **Unreleased** is empty it does nothing.

Format: a release is `## [x.y.z] - YYYY-MM-DD - Optional name`, followed by
`### For everyone` and/or `### Admin and behind the scenes`, each a list of `- `
lines. Lines may use `**bold**`, `` `code` `` and `[links](https://...)`.

## [Unreleased]

### For everyone
- Fewer listings meant for young children or teachers: children's art programs, "Kids Eat Free" nights and teaching-tool demos no longer show. (#91)

### Admin and behind the scenes
- Calendar feeds that leave some events without their own link now import: those events link to the source's page, instead of the whole import failing with a server error. (#93)

## [1.10.0] - 2026-11-19

### For everyone
- The home page drops the line under the headline, so the search and the towns sit higher. (#89)
- Browsers that kept showing the old purple tab icon now pick up the new serif B. (#87)
- A simpler home page: "Find something worth going to." above a bigger town and school search, then a cleaner directory that lists each town with its state and upcoming event count. Schools still work in the search, and a search by school says which school matched. Once the list grows long, a row of letters jumps to towns by their first letter. (#86)
- A smaller footer on one line with Suggest a town, What's new, Privacy and Terms. The version number now lives on What's new. (#86)
- A new browser icon: the serif B from the Bulletin name, dark green on cream, with sharper sizes for tabs, phones and home screens. Shared links show a matching card. (#86)
- A new look for Bulletin: warm paper colours, serif headlines and calmer pages. The home page shows the towns right away as a directory, with how many events are coming up in each. (#85)
- Town pages show events on the first screen. All upcoming, Today and This weekend sit at the top as tabs, search is always there, and the other filters fold behind a **Filters** button that shows how many are on. Events are a dated list with the day set large, the time, the venue and a photo. (#85)
- Event pages lead with what you need: the title, when, where and a button to the original listing, then the photo, the description, the details (now including the category) and sharing. The photo loads first. (#85)
- Easier to use with a keyboard and on phones: every link, button and field shows a clear focus outline, animation stops when your device asks for less motion, buttons are bigger to tap, and nothing scrolls sideways on small screens. (#85)
- `/login` now takes you to the sign-in page. (#85)
- Pages no longer fail with an error now and then while new events are being collected in the background. (#82)
- Fewer listings that aren't events: building open hours, team practices, class sections, study days, "no seminar today" notices, baby and kids' programs, and away games no longer show. (#82)
- Every town now has its own **This weekend** and **Today** pages, and each town page links to other college towns nearby, so it is easier to find something to do (and easier for search engines to send people to the right town). (#78)
- Fewer listings that aren't for students: kids' and family programs, faculty and TA workshops, thesis defenses, and events in other towns (like away meets and county health checks elsewhere) no longer show. Alumni groups and networking nights in town do show. (#65)

### Admin and behind the scenes
- Imports save events in batches instead of committing several times per event, so a source with 500 events imports in about 2.5 seconds of database work instead of 6 to 15, and the site stays responsive while it runs. Imports still run one at a time to stay within the server's memory. (#90)
- Version history updates itself: every night at 10:30 PM Indianapolis time (11:30 PM in summer) a GitHub Actions job turns the notes under Unreleased into a new version, minor if anything is listed for everyone and patch if only admin notes changed, and commits it to `main` so the site redeploys with it. Nights with no new notes do nothing. It can also be run by hand from the Actions tab. (#88)
- The Active cities, Websites and Events tiles on the admin Dashboard open the Cities (active only), Websites and Events pages. A tile is only a link for someone allowed to open that page. (#87)
- After signing in, administrators land on the Dashboard. If sign-in started from a particular admin page, they go back to that page instead (only pages on this site are accepted). The Dashboard shows big numbers with thousands separators, tighter tiles and clearer "nothing here" notes. (#86)
- Admin browser tabs read "Page | Bulletin admin", and event page titles use a colon instead of a middle dot. (#86)
- The stylesheet's colours all go through named tokens, the fonts (DM Serif Display and DM Sans) are served from the site itself so the content security policy needs no font host, and admin pages share the new colours and focus outlines while keeping their layout. (#85)
- New **BiblioCommons library events** pattern: public library calendars on BiblioCommons are read from the platform's events API, 100 events per page, with each event's branch or other venue, address, type and image. Library pages on the platform are recognised automatically. (#84)
- Security check: every package the app uses was confirmed genuine, the minimum versions of Jinja2, python-multipart, Authlib, requests and Starlette were raised past releases with known security problems, the map's Leaflet files from unpkg are now checked against fixed fingerprints so a tampered copy is refused, and CI runs `pip-audit` on every change. No keys or passwords were found in the code or its history. (#83)
- The Simpleview events pattern can read an API that answers with a bare list of events (set `events_root` to an empty string). Library calendars that work this way can now be set up by hand. (#81)
- The admin Websites page can filter by several statuses at once: tick the ones you want to see, or switch to hiding them (for example, everything except Active and Blocked by site). The choice stays in the link, so it can be bookmarked or shared, and older single-status links still work. (#80)
- Town pages are titled and described the way people search ("Things to do in Ann Arbor, Michigan this weekend"), carry breadcrumb and town location data for search engines, and the sitemap lists each town's weekend and today pages. `docs/seo.md` explains what the code does and the steps only the owner can take (custom domain, Search Console, local links). (#78)
- `/health/ready` reports the AI label queue (`checks.ai_categorization`: whether it is on, the provider, how many events are waiting and how many hours the oldest has waited), so outside monitoring can watch it. (#74)
- The admin dashboard shows how many upcoming events are still waiting for an AI category, and how many already have one, when AI labeling is on. (#73)
- AI labeling with Groq no longer stops when the model wraps its answer in an unexpected way, and a bad answer now shows in the logs. (#68)
- `CLAUDE.md` now forbids em dashes in anything written for the project. (#69)
- Errors are reported to Sentry when `SENTRY_DSN` is set: crashes on any page, failing background jobs (scraping, AI labeling, geocoding and the rest) and a scheduler that stops running all raise an alert. Nothing is sent without it. (#71)
- An admin access key (`ADMIN_API_KEY` and `ADMIN_API_KEY_USER_EMAIL`) lets an automated job such as the daily source-maintenance task use the admin pages without a browser sign-in. It acts as the named account, with that account's permissions, and is off unless both are set. (#75)
- A new **Visitors** page (Administration, then Visitors) shows who uses the site: visitors and pages viewed per day, which towns people are looking at, a visitor log (anonymous or the signed-in person's name, towns viewed, device, the site they came from) and recent sign-ins. It sets no cookie, stores no IP address and skips bots. Rows are kept for 180 days (`VISIT_RETENTION_DAYS`). (#79)
- **Detect and configure** no longer fails with a server error on sites styled with utility CSS classes such as `md:tw-px-4`; those classes are left out of generated selectors. (#76)
- Previewing a source no longer fails with a server error when one of its transformation patterns is refused as risky: that rule is skipped and the preview says so. Plain `(?:...)` groups are now allowed, and a truly risky pattern is rejected with a clear message when the configuration is saved. (#77)
- Saving the configure form without touching the hidden settings no longer drops a feed's API endpoint or its allowed content types, so iCalendar sources keep working after an edit. (#77)
- New **Blocked by site** status, in the Problem group of the status filter, for sources whose site refuses our requests. It keeps them together for a later decision on a paid scraping service. (#77)

## [1.9.0] - 2026-11-17 - Version history

### For everyone
- A **What's new** page lists every release, and the site footer shows the current version.
- Eleven more college towns: Durham, Tallahassee, Atlanta, Louisville, Coral Gables, Raleigh, Chapel Hill, South Bend, Pittsburgh, Dallas and Stanford, each with 8 event sources.
- Event page titles now include the town and day, like "Jazz Night · Ann Arbor, Aug 18". (#66)
- A site icon in the browser tab, and a default picture when a page is shared on social media. (#66)
- The footer's **Suggest a town** link goes through sign-in when you are signed out. (#66)

### Admin and behind the scenes
- **Version history** in the admin area (Administration menu) shows every release with all changes, including what is waiting in Unreleased.
- Versions follow semantic versioning; `CHANGELOG.md` is the single source, and `scripts/release.py` cuts a release.
- `/health/ready` reports the running version.
- SEO audit fixes: town pages with fewer than 5 upcoming events are `noindex` and left out of the sitemap; HTTPS-aware redirects; cleaner canonical links for paged listings. (#66)

## [1.8.0] - 2026-11-06 - Share, suggest and My Account

### For everyone
- Share an event to social platforms from its page. (#57)
- Sign in with X. (#58)
- Signed-in visitors can suggest new towns and features. (#59)
- **Saved** and **Alerts** now live under a **My Account** menu in the header. (#63)
- Pages checked at phone to desktop sizes in Chrome, Firefox and Safari's engine, and the layout bugs found were fixed. (#61)
- Events missing a venue now take it from the event's own page. (#62)
- Fewer events that aren't open to you: other campuses and counties, away games, room bookings, alumni and faculty-only events are left out. (#55)
- 29 college towns added one a day since October 10, from Urbana-Champaign to Clemson, each topped up to about 8 event sources.

### Admin and behind the scenes
- Each day's college town and its sources are onboarded automatically from the repo on deploy. (#56)
- The scheduler runs its startup jobs after a deploy hands over leadership. (#64)
- The site no longer runs out of memory when two headless browsers start at once. (#54)
- More location shapes are read when filling venues. (#62)

## [1.7.0] - 2026-10-16 - Alerts inbox and safer, clearer pages

### For everyone
- An **Alerts** inbox for signed-in users. (#48)
- The saved events page was reworked, and past events are greyed out. (#40)
- The events map zooms to the town you chose. (#42)
- Event times show the time zone, and cards name the town only once. (#44)
- Sign in with Yahoo. (#41)
- After signing in you land on the homepage. (#43)
- The site meets WCAG 2.2 AA accessibility guidelines. (#46)
- Class lectures and other non-events are left out, and live events are re-checked daily. (#47)
- Bugs found by deliberately trying to break the app were fixed. (#45, #50)

### Admin and behind the scenes
- Events are categorized with Groq when `GROQ_API_KEY` is set (#49); the default model is `gpt-oss-120b` (#51), and every event was re-labeled by bumping the prompt version. (#52)
- A test user for dev and staging, and a setup guide for each sign-in provider. (#41, #43)
- CI now really runs the tests on PostgreSQL. (#50)
- Paged JSON event lists next to a meta block are read. (#53)

## [1.6.0] - 2026-10-14 - Smarter scraping

### For everyone
- Away golf tournaments and other out-of-town events are swept out automatically. (#33)

### Admin and behind the scenes
- AI categorization retries no longer burn the daily Gemini quota (#32), and repeated Gemini 503s back off longer with an optional fallback model. (#39)
- At most one headless browser per process, and no more route-close tracebacks. (#34)
- New extraction patterns: paged JSON and Campus Labs Engage, with fixes to detection and recovery. (#35)
- Previewed drafts can be approved directly, and browser walks read any record root. (#36)
- Faster card detection, clearer browser errors, and Laravel-style paged JSON. (#37)
- Recovery handles self-navigating pages and combined date-time cards. (#38)

## [1.5.0] - 2026-10-09 - Map, social sign-in and accessibility

### For everyone
- Sign in with Google, Microsoft or Facebook only. Bulletin no longer stores passwords. (#11, #12, #13)
- A redesigned sign-in page with provider logos. (#18)
- The events map shows events. (#14)
- AI tools and calendar apps can read the site, including a calendar feed for each town. (#10)
- The site meets WCAG 2.1 AA accessibility guidelines. (#17)
- Event pages show **Saved** and **Following**, with **Unsave** and **Unfollow** on hover. (#24)
- Event descriptions come from each event's own page when the listing has none. (#30)
- Events for college students show once, in the school's own town. (#19)
- Cards no longer repeat the town name. (#21)
- Fewer non-events: away games, study and advising hours, info sessions, lab meetings, reading groups and more are left out. (#25, #27)

### Admin and behind the scenes
- The AI categorizer gets category descriptions and a keep/drop check, and spends the daily Gemini quota on upcoming, visible events first. (#15, #20)
- Repeat listings across sources are flagged to admins as possible duplicates. (#20)
- Out-of-town addresses with no state are caught. (#21)
- Geocoding does upcoming events first, matches campus venues, and explains why events are skipped; the Map locations page is easier to read. (#16, #22, #26, #28, #31)
- Only one admin menu opens at a time. (#29)
- The repo was split into `backend/`, `frontend/` and `docs/`, with guides for AI coding tools. (#23)

## [1.4.0] - 2026-10-06 - Bulletin

### For everyone
- The site is now called **Bulletin**, and a town can have several universities.
- Hide the categories or sources you don't want to see.
- A much larger pool of placeholder photos.
- Friendly error pages, a privacy policy and terms of use. (#9)
- Account deletion and sign-in limits. (#9)
- Events nobody can actually go to are left out. (#8)

### Admin and behind the scenes
- A universities count on the admin dashboard.
- Security fix: a scraped title could inject markup through the event's structured data. (#8)
- The map endpoint no longer returns every city at once. (#8)
- CI lint, PostgreSQL migrations and the test suite pass, and the suite runs in minutes instead of an hour. (#8)
- Unused tables, columns and files removed. (#8)

## [1.3.0] - 2026-09-24 - College-town chooser

### For everyone
- Pick a college town instead of scrolling one feed of every city, with suggestions as you type.
- Pages carry search-engine metadata.

### Admin and behind the scenes
- A city's university can be set in the admin.
- The API docs are no longer public, and production uses the public base URL.

## [1.2.0] - 2026-09-17 - Cleaner admin

### For everyone
- The account page stops advertising features that already shipped as "coming soon".

### Admin and behind the scenes
- The admin UI was condensed and repaired, and its confirmation prompts work again under the security policy.
- Policy, decision, report and dashboard pages were made readable.

## [1.1.0] - 2026-09-11 - Photos and categories

### For everyone
- Events without a photo get a category-relevant picture.
- Events are sorted into categories automatically.

### Admin and behind the scenes
- Categorization runs from the scheduler: starter keyword rules, then Gemini AI, stopping gracefully at the daily quota.
- A whole-site link and button checker runs in the test suite.

## [1.0.0] - 2026-09-03 - Live on Render

### For everyone
- Bulletin's first production release.

### Admin and behind the scenes
- Render deploy kit (Docker, persistent disk), and the old app was removed.
- The web server stays up if the scheduler crashes.

## [0.7.0] - 2026-09-02 - Better extraction

### For everyone
- Event dates show the year, and recurring events are marked.

### Admin and behind the scenes
- Onboarding falls back to a browser for protected sites, and reads modern LiveWhale calendars, schema.org ItemLists, inline JavaScript events and nested fields.
- Recurring events are expanded, day-first and year-less dates are understood, and more pagination styles are followed.

## [0.6.0] - 2026-08-07 - Admin import tools

### Admin and behind the scenes
- A clearer website workflow in the admin.
- Captured request recipes and browser-backed event imports, with pagination.
- Import scheduling controls.

## [0.5.0] - 2026-07-30 - Feature complete

### For everyone
- Full public event pages, saved events and alerts.
- Sign in with an outside account.

### Admin and behind the scenes
- Automatic onboarding controlled by policies, with an optional AI fallback.
- JSON-in-script, ICS, RSS/Atom, Algolia, Simpleview and browser extraction.
- A durable background scheduler, geocoding and operational reports.
- Production preparation: Redis rate limiting and PostgreSQL in CI.

## [0.4.0] - 2026-07-21 - Extraction engine

### For everyone
- Public event pages that work on phones.

### Admin and behind the scenes
- A deterministic extraction engine, configuration inference and a bulk onboarding queue.

## [0.3.0] - 2026-07-13 - The new app

### For everyone
- Create an account and manage it.

### Admin and behind the scenes
- Rebuilt from scratch on FastAPI: database schema, permissions, and admin pages for cities, websites and events, with event review, categories and duplicate detection.

## [0.2.0] - 2026-06-15 - First scrapers

### For everyone
- A home page with events from Bloomington and Bethlehem, filterable by category.

### Admin and behind the scenes
- The first scrapers, with end dates and purging of internal events.

## [0.1.0] - 2026-05-24 - First commit

### Admin and behind the scenes
- The project started.
