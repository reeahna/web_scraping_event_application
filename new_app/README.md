# Bulletin

A college-town events site. It imports event listings from local sources
(venue sites, university calendars, tourism boards, Eventbrite pages), keeps
them up to date on a schedule, and shows them town by town. Built with
FastAPI, SQLAlchemy/Alembic, Jinja2 and SQLite, with Playwright for sources that
need a real browser.

For deploying, see [`../DEPLOY_RENDER.md`](../DEPLOY_RENDER.md).

## Running it locally

```bash
cd new_app
python -m venv venv
venv\Scripts\activate            # Windows; on macOS/Linux: source venv/bin/activate
pip install -e ".[dev]"
python -m playwright install chromium
python -m alembic upgrade head
python scripts/create_superadmin.py --email you@example.com --password "..."
uvicorn app.main:app --reload --port 8100
```

The site is then at http://localhost:8100 and the admin at `/admin`.

Scraping runs in a separate process, never inside the web server. To have
imports run on their schedule locally, start it in a second terminal:

```bash
python -m app.scheduler
```

In production (`start.sh`) both run in one container, and the scheduler is
restarted on its own if it ever exits.

## How it fits together

**Public site.** `/` is a town chooser with type-ahead (by town, state or
university); there is deliberately no all-towns feed. `/city/{slug}` lists a
town's upcoming events with search, category/source/date filters, "Today" and
"This weekend" presets, a list/map toggle, and a "Hide categories or sources"
panel for leaving things out. `/events/{id}` is the event page. Events without
their own image get a category-matched Unsplash photo.

**For search engines and AI tools.** Pages are server-rendered, event and town
pages carry schema.org JSON-LD (an `Event` per event, an `ItemList` per town),
and `/sitemap.xml` lists every public page. `/robots.txt` names the main AI
crawlers (GPTBot, ClaudeBot, Google-Extended, ...) in the same group as `*`, so
they get the public pages and the same private-area rules. `/llms.txt` is a
plain-text guide to the site, and each town has an iCalendar feed at
`/city/{slug}/events.ics` that calendar apps can subscribe to.

**Accounts.** Anyone can sign up at `/register`. A new account gets only the
**Registered User** role, which has no admin permissions. Signed-in users can
save events (`/account/saved`), follow towns, set alert preferences
(`/account/alerts`), and delete their account from `/account`. Google, Microsoft
and Facebook sign-in switch on when their client IDs are set. Failed logins are
limited (10 per account and 30 per address in 15 minutes).

Password reset (`/auth/forgot-password`) and email confirmation switch on by
themselves once email sending is configured; until then the links to them are
hidden and nothing is blocked on an unconfirmed address.

The privacy policy and terms are at `/privacy` and `/terms`.

**Admin** (`/admin`). Roles are Super Administrator, Administrator, Editor and
Registered User, and every admin action is permission-checked and audited.
Only a Super Administrator can grant the two administrator roles, and the last
active Super Administrator cannot be removed.

- *Towns* (`/admin/cities`): towns, their universities, and whether they are
  shown.
- *Sources* (`/admin/websites`): each website scraped for a town. Adding one
  runs detection (which known listing format is it?), proposes a configuration,
  previews the events it would import, and waits for approval before anything is
  stored. Bulk onboarding accepts many URLs or a CSV at once, and
  auto-onboarding policies decide how much of that runs without a person.
- *Events* (`/admin/events`): review, archive, fix a category or a location,
  and resolve possible duplicates. Scraped fields (title, dates, URL and so on)
  are never edited by hand; corrections are stored alongside them.
- *Categories and rules*: 14 seeded categories and keyword/venue/source rules
  that assign them. With a Gemini key, the scheduler also labels new events
  with AI.
- *Scheduler*, *Reports*, *Notifications*: run status, failures, and the health
  of each source.

**Importing.** Every source is described by data, not code: one of 13 listing
formats (JSON-LD, WordPress, The Events Calendar, LiveWhale, Simpleview, ICS,
RSS, generic HTML cards, and others) plus selectors and settings. The pipeline
is fetch → detect → extract → normalize → validate → de-duplicate → store. A
source that only renders in a browser falls back to a restricted headless
Chromium. Sources that fail repeatedly are flagged for review.

**What counts as an event.** Imports leave out anything a person cannot
actually go to: online-only sessions, conferences, certification courses,
internal staff/faculty business ("faculty meeting", "employees only", "by
invitation"), and calendar entries that are deadlines or reminders ("Last day
to drop", "Registration closes", "Fall Break"). The rules are in
`app/services/attendability.py`. The site is for college students, so imports
also leave out what is aimed at someone else (business networking, finance and
real-estate masterclasses, research seminars, toddler story times, class reunions), room
bookings and class sections a campus calendar publishes, and listings marked
cancelled. Each import run records how many it left out
(`not_attendable_excluded:N`), and an event imported before the filter existed
is taken down the next time its source lists it.

**Only the source's own town.** An event whose address names another town
(an Eventbrite Bloomington page lists Indianapolis events too) is left out and
counted as `outside_home_town_excluded:N`. Events with no town in their address
are kept. A source with its own geographic filter is left to that filter. See
`app/services/home_town.py`.

**Listed once.** When several sources list the same title on the same day in
the same town, the public listings, counts, map and sitemap show it once. The
other copies keep their own page and still show under their own source's
filter.

## Configuration

Settings come from environment variables or `new_app/.env`. Copy
`.env.example` to start. Everything optional is off by default, and the app runs
without any of it.

| Variable | Default | What it does |
|---|---|---|
| `DATABASE_URL` | `sqlite:///new_app/app.db` | Database |
| `APP_ENV` | `development` | `production` turns on the readiness checks at `/health/ready` and hides the API docs |
| `APP_NAME` | `Bulletin` | Site name shown everywhere |
| `PUBLIC_BASE_URL` | `http://localhost:8100` | Used for canonical links and the sitemap. On Render it is picked up automatically |
| `APP_TIMEZONE` | `UTC` | Decides what "today" means for the listings |
| `REGISTRATION_ENABLED` | `true` | Public sign-up |
| `BROWSER_EXTRACTION_ENABLED` | `false` | Headless-browser fallback for sources that need it |
| `ATTENDABILITY_FILTER_ENABLED` | `true` | Leave out non-events (see above) |
| `UNSPLASH_ACCESS_KEY` | unset | Placeholder photos for imageless events |
| `GEMINI_API_KEY` | unset | AI categorization of new events |
| `GEOCODING_ENABLED`, `GEOCODING_PROVIDER` | off | Fill in map coordinates from addresses |
| `EMAIL_ENABLED`, `EMAIL_BACKEND` | off | Alerts, password reset and email confirmation |
| `CONTACT_EMAIL` | unset | Shown on the privacy policy and terms |
| `GOOGLE_/MICROSOFT_/FACEBOOK_CLIENT_ID` and `_SECRET` | unset | Social sign-in |
| `COOKIE_SECURE`, `BEHIND_HTTPS`, `TRUSTED_HOSTS` | off | Set in production (already in `render.yaml`) |
| `RATE_LIMIT_BACKEND`, `REDIS_URL` | `memory` | Use `redis` to share rate limits across processes |

## Scripts

Run from `new_app/` with the venv active.

| Script | Use |
|---|---|
| `scripts/create_superadmin.py` | Create the first admin login |
| `scripts/hide_unattendable_events.py` | List (or with `--apply`, deactivate) existing events the import filters would now leave out, including out-of-town ones |
| `scripts/categorize_events.py` | Seed the starter category rules and re-run them over every event |
| `scripts/categorize_events_ai.py` | Label events with Gemini (`--only-other`, `--force`, `--limit N`) |
| `scripts/fetch_category_photos.py` | Refill the placeholder photo pool now (the scheduler also does this when it is small) |
| `scripts/check_links.py` | Crawl the whole site for broken links and dead form targets |

## Tests and checks

```bash
python -m ruff check .
python -m pytest            # about 2-3 minutes
```

Tests use their own database (`tests/test_app.db`). CI
(`.github/workflows/ci.yml`) runs Ruff, a migration round trip, and the suite on
every pull request and every push to `main`, plus the migrations against
PostgreSQL.

After changing a model, add a migration (`python -m alembic revision -m "..."`)
and check it both ways:

```bash
python -m alembic upgrade head
python -m alembic downgrade -1
python -m alembic upgrade head
```
