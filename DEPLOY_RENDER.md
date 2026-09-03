# Deploying to Render

This app runs as **one always-on Docker container** on Render: the FastAPI web
server and the background scheduler run together, sharing one SQLite database on
a **persistent disk**. The restricted headless browser (Chromium/Playwright) is
built into the image.

Everything is already configured in [`render.yaml`](render.yaml) and
[`new_app/Dockerfile`](new_app/Dockerfile). You just connect the repo.

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
3. Render reads `render.yaml`, shows the **bethlehem-events** web service and a
   1 GB disk. Click **Apply**.
4. First build takes ~5–10 min (it installs Chromium). Watch the deploy log.

When it finishes you get a URL like `https://bethlehem-events.onrender.com`
with HTTPS already set up.

### 3. Create your admin login
The database starts empty. Open the service in Render → **Shell** tab → run:

```bash
python scripts/create_superadmin.py --email you@example.com --password "a-strong-password"
```

(That script is idempotent — re-running it just resets the password.)

Then visit your URL and log in.

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

- `new_app/Dockerfile` — Python 3.13 + Chromium/Playwright, installs
  `requirements.txt`, copies the app, runs `start.sh`.
- `new_app/start.sh` — runs migrations, then launches the scheduler and uvicorn
  together; if either dies the container restarts (keeping them in lockstep).
- `render.yaml` — the web service, the persistent disk mounted at `/data`, and
  the production env vars.
