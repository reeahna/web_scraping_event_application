# Changelog

Bulletin's version history: the new features and capabilities each release added,
newest first. The app reads this file to build two pages:

- **What's new** (`/whats-new`), for everyone: only the **For everyone** sections.
- **Version history** (`/admin/version-history`), for admins: everything,
  including **Admin and behind the scenes** and anything still **Unreleased**.

This is not a log of every change. Git history and the pull requests are the full
technical record; this file only announces things that are genuinely new.

Version numbers follow Bulletin's own convention (shaped like semantic versioning,
but not strict SemVer): the first number goes up for a big change in how the site
works, the second for a release with something new for everyone, the third for a
release that only adds admin tools. The newest released version below is the app's
version and shows on What's new.

## What belongs here

Add a line only when a pull request gives someone something new to do or see in
Bulletin. Ask: "What can a visitor (or an admin) do now that they couldn't before?"
If the honest answer is "the same thing, but it works properly now", it does not
belong here.

Belongs here:

- New features people can use, and new pages, tools or sections of the site.
- New capabilities, such as a new way to sign in, a new feed or a new filter.
- Meaningful additions to event discovery (a new map, new towns, new views).
- A user-facing change big enough to be something new (a whole redesign).
- A new integration, when it gives people a service they didn't have.

Does not belong here, however big or important:

- Bug fixes, including serious ones, and changes that make an existing feature
  work correctly (time zones, dates, wrong times, crashes, missing fields).
- Scraper fixes, filter tweaks that hide more non-events, source-by-source
  improvements and reliability work.
- Refactoring, maintenance, performance, dependency and security updates.
- Database migrations, CI, tests, docs and other internal changes.
- Small wording, layout or styling tweaks to something that already exists.

Examples: adding event saving belongs; fixing a bug in saving does not. Adding an
events map belongs; fixing the map's time zone maths does not. Showing when
listings were last checked belongs; fixing the date calculations behind it does not.

Most pull requests add nothing here, and a release can have no What's new entry at
all. Both are fine.

## How to keep it current

1. When a pull request adds something that belongs here, add one line under
   **Unreleased**: under **For everyone** if a visitor or signed-in user gets it,
   under **Admin and behind the scenes** if it is a new admin page, tool or
   capability. Describe what the reader can now do, in plain words for that
   reader, rather than copying the PR title or commit message. End it with the PR
   number, like `(#67)`. The public page hides PR numbers. Keep public and admin
   news apart: the admin half of a public feature does not need its own line.
2. Every night at 10:30 PM Indianapolis time (11:30 PM in summer) a GitHub
   Actions job (`.github/workflows/nightly-release.yml`) runs
   `python scripts/release.py auto`, which turns **Unreleased** into a new
   version (minor when there are **For everyone** notes, patch when there are only
   admin notes) and commits it to `main`. It only moves lines that are already
   here, so the editorial decision is made in step 1. When **Unreleased** is empty
   it does nothing. To cut a release by hand, run `python scripts/release.py
   minor|patch|major` from `backend/` and commit both files.

Format: a release is `## [x.y.z] - YYYY-MM-DD - Optional name`, followed by
`### For everyone` and/or `### Admin and behind the scenes`, each a list of `- `
lines. Lines may use `**bold**`, `` `code` `` and `[links](https://...)`.

## [Unreleased]

### For everyone
- Town pages, including Today and This weekend, now say when their listings were last checked, in the town's own time (for example "Listings checked November 19, 2026 at 8:45 PM EST"), and event pages say when that event's details last changed. (#100)

### Admin and behind the scenes
- **Preview recheck** on a LiveWhale source's admin page (under Advanced tools): reads current events that show 12:00 AM, have no venue or were missed by the latest import back from the source one by one, lists the corrections it would make, and applies them only when you choose to. (#104)
- Admins can open **Events > AI labeling** to see what AI event labeling is doing: how many events are waiting, the tokens each request used, which free-tier limit is slowing it down, and the events the AI couldn't label, with a button to ask it again. (#105)
- From **Events > AI labeling**, admins can run a model evaluation that labels about 100 sample events with the current AI setup, a leaner one and a cheaper model, then shows side by side where they agree on category and on hiding an event, which events a setup would wrongly hide, and how many tokens each uses. It never changes an event. (#107)
- AI event labeling can use a backup model (`GROQ_FALLBACK_MODEL`, off unless set) while the main model's free daily quota is used up. The backup only gives provisional categories to events that have none from the main model yet: it never hides an event or changes one the main model or an admin has already decided, and the main model checks every one later. **Events > AI labeling** and each event's page show which categories are provisional. (#PRNUM)

## [1.11.0] - 2026-11-20

### Admin and behind the scenes
- Admin Notifications has a **Mark all as read** button that clears every unread notification at once. (#92)

## [1.10.0] - 2026-11-19

### For everyone
- A new look for Bulletin: warm paper colours, serif headlines and calmer pages. The home page is a directory of every town with how many events are coming up in each, under one big search for a town or school, and a row of letters jumps through a long list of towns. (#85, #86)
- Town pages show events on the first screen: All upcoming, Today and This weekend sit at the top as tabs, search is always there, and the other filters fold behind a **Filters** button that shows how many are on. (#85)
- Every town now has its own **This weekend** and **Today** pages, and each town page links to other college towns nearby. (#78)

### Admin and behind the scenes
- A new **Visitors** page (Administration, then Visitors) shows who uses the site: visitors and pages viewed per day, which towns people are looking at, a visitor log and recent sign-ins. It sets no cookie, stores no IP address and skips bots. (#79)
- New **BiblioCommons library events** pattern: public library calendars on BiblioCommons are read from the platform's events API, with each event's branch or venue, address, type and image. (#84)
- The admin Websites page can filter by several statuses at once, or hide them (for example, everything except Active and Blocked by site). The choice stays in the link, so it can be bookmarked or shared. (#80)
- New **Blocked by site** status for sources whose site refuses our requests, keeping them together for a later decision on a paid scraping service. (#77)
- Errors are reported to Sentry when `SENTRY_DSN` is set: crashes on any page, failing background jobs and a scheduler that stops running all raise an alert. (#71)
- An admin access key (`ADMIN_API_KEY` and `ADMIN_API_KEY_USER_EMAIL`) lets an automated job use the admin pages without a browser sign-in, acting as the named account with its permissions. (#75)

## [1.9.0] - 2026-11-17 - Version history

### For everyone
- A **What's new** page lists what each release added and shows the current version.
- Eleven more college towns: Durham, Tallahassee, Atlanta, Louisville, Coral Gables, Raleigh, Chapel Hill, South Bend, Pittsburgh, Dallas and Stanford.

### Admin and behind the scenes
- **Version history** in the admin area (Administration menu) shows every release, including what is waiting in Unreleased.

## [1.8.0] - 2026-11-06 - Share, suggest and more towns

### For everyone
- Share an event to social platforms from its page. (#57)
- Sign in with X. (#58)
- Signed-in visitors can suggest new towns and features. (#59)
- 29 college towns added one a day since October 10, from Urbana-Champaign to Clemson.

## [1.7.0] - 2026-10-16 - Alerts inbox

### For everyone
- An **Alerts** inbox for signed-in users. (#48)
- Sign in with Yahoo. (#41)

## [1.6.0] - 2026-10-14 - Smarter scraping

### Admin and behind the scenes
- New extraction patterns for paged JSON feeds and Campus Labs Engage. (#35)
- Previewed drafts can be approved directly. (#36)

## [1.5.0] - 2026-10-09 - Map and social sign-in

### For everyone
- Sign in with Google, Microsoft or Facebook. Bulletin no longer stores passwords. (#11, #12, #13)
- The events map shows events. (#14)
- AI tools and calendar apps can read the site, including a calendar feed for each town. (#10)

### Admin and behind the scenes
- Repeat listings across sources are flagged to admins as possible duplicates. (#20)

## [1.4.0] - 2026-10-06 - Bulletin

### For everyone
- The site is now called **Bulletin**, and a town can have several universities.
- Hide the categories or sources you don't want to see.
- Delete your account. (#9)

## [1.3.0] - 2026-09-24 - College-town chooser

### For everyone
- Pick a college town instead of scrolling one feed of every city, with suggestions as you type.

### Admin and behind the scenes
- A city's university can be set in the admin.

## [1.1.0] - 2026-09-11 - Photos and categories

### For everyone
- Events without a photo get a category-relevant picture.
- Events are sorted into categories automatically.

## [1.0.0] - 2026-09-03 - Live on Render

### For everyone
- Bulletin's first production release.

## [0.7.0] - 2026-09-02 - Better extraction

### Admin and behind the scenes
- Onboarding falls back to a browser for protected sites, and reads modern LiveWhale calendars, schema.org ItemLists and events in inline JavaScript.

## [0.6.0] - 2026-08-07 - Admin import tools

### Admin and behind the scenes
- Captured request recipes and browser-backed event imports, with pagination.
- Import scheduling controls.

## [0.5.0] - 2026-07-30 - Feature complete

### For everyone
- Full public event pages, saved events and alerts.
- Sign in with an outside account.

### Admin and behind the scenes
- Automatic onboarding controlled by policies, with an optional AI fallback.
- JSON-in-script, ICS, RSS/Atom, Algolia, Simpleview and browser extraction.
- A background scheduler that keeps sources up to date, geocoding for the map, and operational reports.

## [0.4.0] - 2026-07-21 - Extraction engine

### For everyone
- Public event pages that work on phones.

### Admin and behind the scenes
- A deterministic extraction engine, configuration inference and a bulk onboarding queue.

## [0.3.0] - 2026-07-13 - The new app

### For everyone
- Create an account and manage it.

### Admin and behind the scenes
- Admin pages for cities, websites and events, with event review, categories and duplicate detection.

## [0.2.0] - 2026-06-15 - First scrapers

### For everyone
- A home page with events from Bloomington and Bethlehem, filterable by category.

### Admin and behind the scenes
- The first scrapers.

## [0.1.0] - 2026-05-24 - First commit

### Admin and behind the scenes
- The project started.
