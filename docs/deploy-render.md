# Deploying to Render

This app runs as **one always-on Docker container** on Render: the FastAPI web
server and the background scheduler run together, sharing one SQLite database on
a **persistent disk**. The restricted headless browser (Chromium/Playwright) is
built into the image.

Everything is already configured in [`render.yaml`](../render.yaml) and
[`backend/Dockerfile`](../backend/Dockerfile). You just connect the repo.

---

## One-time setup (~10 minutes)

### 1. Push the latest code to GitHub
The repo is already at `github.com/reeahna/web_scraping_event_application`. Make
sure your latest commits are pushed:

```bash
git push origin main
```

Render deploys from GitHub, so anything not pushed won't be deployed.

### 2. Create the service from the Blueprint
1. Go to **dashboard.render.com** → **New +** → **Blueprint**.
2. Connect your GitHub account and pick this repository.
3. Render reads `render.yaml`, shows the **city-events** web service and a
   1 GB disk. Click **Apply**.
4. First build takes ~5–10 min (it installs Chromium). Watch the deploy log.

When it finishes you get a URL like `https://city-events.onrender.com`
with HTTPS already set up.

### 3. Set up sign-in and your admin account
There are no passwords: people sign in with Google, Microsoft, Facebook or Yahoo.

1. In the Google Cloud console (**APIs & Services → Credentials**), create an
   **OAuth client ID** of type *Web application*. Add the authorized redirect URI
   `https://<your-service>.onrender.com/auth/oauth/google/callback`.
2. In Render → the service → **Environment**, add `GOOGLE_CLIENT_ID`,
   `GOOGLE_CLIENT_SECRET`, and `SUPERADMIN_EMAIL` (your Google address).
3. Visit your URL, click **Sign in → Google** with that address, and you land in
   the admin as Super Administrator.

Microsoft, Facebook and Yahoo work the same way: create an app with the
provider, register its callback URL, and add its ID and secret in Render. Each
button appears on the sign-in page once both values are set. Only a Google
sign-in can claim `SUPERADMIN_EMAIL`, because Google confirms the address.

| Provider | Where to create the app | Callback URL to register | Render variables |
|---|---|---|---|
| Microsoft | [Microsoft Entra admin center](https://entra.microsoft.com) → **App registrations → New registration**. Supported account types: *Accounts in any organizational directory and personal Microsoft accounts*. Platform: *Web*. Then **Certificates & secrets → New client secret**. | `…/auth/oauth/microsoft/callback` | `MICROSOFT_CLIENT_ID` (the *Application (client) ID*), `MICROSOFT_CLIENT_SECRET` (the secret's *Value*) |
| Facebook (Meta) | [Meta for Developers](https://developers.facebook.com/apps) → **Create app** → use case *Authenticate and request data from users with Facebook Login*. Under **Facebook Login → Settings**, add the callback as a *Valid OAuth Redirect URI*; under **Use cases → Customize**, add the `email` permission. Switch the app to **Live** so people other than you can sign in. | `…/auth/oauth/facebook/callback` | `FACEBOOK_CLIENT_ID` (*App ID*), `FACEBOOK_CLIENT_SECRET` (*App secret*, under **App settings → Basic**) |
| Yahoo | [Yahoo Developer apps](https://developer.yahoo.com/apps/) → **Create an App**. Application type: *Web Application*. Under **API Permissions → OpenID Connect Permissions**, tick *Email* and *Profile*. | `…/auth/oauth/yahoo/callback` (Yahoo only accepts an `https://` address, so local `http://localhost` sign-in can't be tested with Yahoo) | `YAHOO_CLIENT_ID` (*Client ID*), `YAHOO_CLIENT_SECRET` (*Client Secret*) |

`…` is your site's address, for example `https://<your-service>.onrender.com`.

### 4. (Optional) AI event categorization
The keyword rules categorize events for free, but Gemini labels them much more
accurately (it knows a "cooking class" is food, not education). Get a free key
at https://aistudio.google.com/apikey, add it in the Render dashboard under the
service's **Environment** as `GEMINI_API_KEY`, then from the **Shell** run:

```bash
python scripts/categorize_events_ai.py
```

Re-running it is safe and cheap — it only spends API calls on events it has not
labeled yet. Use `--only-other` to relabel just the "Other" pile, or `--force`
to redo everything. New events keep getting the instant keyword category on
import; run this again whenever you want to upgrade the backlog to AI labels.

---

## Updating the app later
Just push to `main`:

```bash
git push origin main
```

`autoDeploy` is on, so Render rebuilds and redeploys automatically. Database
migrations run on every start (`alembic upgrade head` in `start.sh`), so schema
changes apply themselves.

---

## Good to know

- **Cost:** ~$7/mo for the `starter` web service + ~$0.25/mo for the 1 GB disk.
  Do **not** switch to the free plan — it sleeps when idle, which would stop the
  scheduler from importing events.
- **Memory:** `starter` has 512 MB for the web server, the scheduler and
  headless Chromium together. One browser render takes roughly 300 MB, so
  `BROWSER_MAX_CONCURRENT` (default 1) keeps renders one at a time. If Render
  still reports the instance running out of memory, move to `standard` (2 GB).
- **The database** lives at `/data/app.db` on the persistent disk and survives
  deploys and restarts. It is never baked into the image.
- **Backups:** from the Render **Shell**, `cp /data/app.db /data/app.db.bak`
  before risky changes; download it with the Render CLI if you want an offsite
  copy. (For heavier durability later, switch `DATABASE_URL` to a managed
  Postgres — no app code changes needed.)
- **A custom domain** (e.g. `events.yourdomain.com`): add it under the service's
  **Settings → Custom Domains**, then add the CNAME Render shows you at your DNS
  provider. HTTPS is issued automatically. (Optionally set a `TRUSTED_HOSTS`
  env var to that hostname to enable host-header validation.)
- **Config** is all environment variables (set in `render.yaml`, editable in the
  dashboard). Notable ones: `DATABASE_URL`, `BROWSER_EXTRACTION_ENABLED=true`,
  `APP_ENV=production`, `COOKIE_SECURE=true`.

---

## How it's wired (for reference)

- `backend/Dockerfile` — Python 3.13 + Chromium/Playwright, installs
  `requirements.txt`, copies `backend/` and `frontend/`, runs `start.sh`.
- `backend/start.sh` — runs migrations, then launches the scheduler and uvicorn
  together; if either dies the container restarts (keeping them in lockstep).
- `render.yaml` — the web service, the persistent disk mounted at `/data`, and
  the production env vars.
