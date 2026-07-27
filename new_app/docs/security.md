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
  token is minted on every login (local and OAuth) — session-fixation safe.
- Cookies: `HttpOnly`, `SameSite=Lax`, `Secure` in production (`COOKIE_SECURE`).
- CSRF: double-submit token on every state-changing form/POST (`app/core/csrf`).
- Password floor via `MINIMUM_PASSWORD_LENGTH`; local login can be disabled
  entirely (`LOCAL_LOGIN_ENABLED=false`) once OAuth is the sole path.
- Registered users hold **zero** administrative permissions by default.

## OAuth (Phase 14)

Authlib-based; providers are credential-gated (disabled without id+secret).
State is one-time and expiring; OIDC nonce is carried through; account linking
happens only on a provider-**verified** email; an unverified email never
hijacks an existing account; disabled users are rejected; redirects are
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
