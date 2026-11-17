# Security

## Transport & headers

- TLS terminated upstream; set `BEHIND_HTTPS=true`. `SecurityHeadersMiddleware`
  then adds HSTS plus, always: a Content-Security-Policy, `X-Frame-Options:
  DENY` (clickjacking), `X-Content-Type-Options: nosniff`, and a referrer
  policy. The CSP (`CONTENT_SECURITY_POLICY`) restricts scripts/styles to self
  plus the pinned Leaflet CDN and forbids framing (`frame-ancestors 'none'`).
- `TRUSTED_HOSTS` enables Host-header validation (`TrustedHostMiddleware`).
- `MaxBodySizeMiddleware` rejects oversized requests (413) by Content-Length.

## Sessions & auth

- Session tokens are random; only their hash is stored (`UserSession`). A new
  token is minted on every login — session-fixation safe.
- Cookies: `HttpOnly`, `SameSite=Lax`, `Secure` in production (`COOKIE_SECURE`).
- CSRF: double-submit token on every state-changing form/POST (`app/core/csrf`).
- Sign-in is social only (Google, Microsoft, Facebook, X, Yahoo). There is no password
  login, so no password, reset token or verification token is ever stored.
- `SUPERADMIN_EMAIL` grants Super Administrator, but only on a sign-in whose
  provider has verified that address (Google). Microsoft, Facebook, X and Yahoo sign-ins
  can't claim it.
- Registered users hold **zero** administrative permissions by default.

## OAuth (Phase 14)

Authlib-based; providers are credential-gated (disabled without id+secret).
State is one-time and expiring; OIDC nonce is carried through; account linking
happens only on a provider-**verified** email; an unverified email never
hijacks an existing account, and when a verified sign-in joins an account any
unverified sign-in already on it is unlinked; disabled users are rejected; redirects are
allowlisted to local paths. No third-party password or provider token is stored.

## SSRF & outbound safety

Every outbound fetch (HTTP and the browser strategy) is SSRF-validated —
private/loopback/non-http(s) targets are refused, and the browser re-validates
every subrequest. Challenge/login walls are reported blocked, never solved.

## Secrets

- Never in `SiteConfiguration` (schema rejects env-var references and raw
  secrets). API keys reach outbound headers only via `secret_header_refs`
  (`env:NAME`), resolved at request time and never stored, logged, or audited.
- Provide secrets via environment/secret manager; never commit `.env`.
- **Rotation**: change the value in the secret store and restart the affected
  processes; nothing caches secrets on disk.

## Admin access key

An automated job (the daily source-maintenance task) can use the admin pages
without an OAuth sign-in by sending `Authorization: Bearer <ADMIN_API_KEY>`.

- Off unless both `ADMIN_API_KEY` (at least 32 characters; a shorter value is
  ignored) and `ADMIN_API_KEY_USER_EMAIL` are set.
- The request acts as that existing, active account: its roles decide what is
  allowed, and its audit trail records what was done. Use an account with only
  the permissions the job needs.
- It replaces only the session cookie. Forms still need the CSRF token, which
  the job reads from the cookie a page load sets, like a browser.
- The key is compared in constant time and never logged. A wrong key, or a
  missing or disabled account, is refused like a signed-out request.
- Keep it in Render's environment settings and the job's own secret store, never
  in the repository or a chat. Rotate by changing it in both places.

## Redaction

- Logs and the operational dashboard surface counts and safe fields only —
  never secrets, cookies, auth headers, tokens, credentials, raw source
  content, audit before/after payloads, or another user's preferences.
- Error responses are generic; correlation IDs tie a user-visible error to
  server logs without leaking internals.

## Rate limiting

Development uses an in-process limiter (not production-grade). Production must
use a shared backend (`RATE_LIMIT_BACKEND=redis`, `REDIS_URL`); `/health/ready`
flags the in-process limiter as a blocker.

## Compliance

- **robots.txt / source terms**: onboarding is per-source and admin-reviewed;
  review each source's terms and robots directives before approving. Respect
  configured rate limits (`FetchConfig.rate_limit_delay_seconds`, the schedule
  interval floor of 15 minutes).
- **Nominatim**: geocoding is disabled by default; when enabled it sends a
  descriptive User-Agent and rate-limits to ≤1 request/second per Nominatim's
  usage policy, and caches by address hash to minimise requests.
- **Provider terms**: OAuth and any AI provider are used only under their
  terms and only when explicitly configured.
- **Privacy / deletion / retention**: see `docs/operations.md`.
