# Putting Cloudflare (free plan) in front of radreport

What the app already does, so the CDN only has to respect it:

| Response | `Cache-Control` the app sends | What the CDN should do |
|---|---|---|
| `/ui/static/*?v=<hash>` (every page links this form) | `public, max-age=31536000, immutable` | cache for a year at the edge |
| `/ui/static/*` without `v` | `public, max-age=300` | cache briefly |
| every page and API answer | `private, no-store` | never cache, never store |
| dictation audio | the app answers `307` to a signed S3 URL valid for 60 s; S3 is told to answer `private, no-store` | never sees it: the browser fetches from the bucket directly |

A release changes a file's hash, so the `v=` in every page changes and nothing ever needs purging.

## Setup (about an hour)

1. Add the site to Cloudflare and move DNS; proxy (orange cloud) only the app's hostname.
2. **SSL/TLS** → Full (strict). Origin keeps its own certificate.
3. **Caching → Cache Rules**, in this order:
   1. *Static assets*: `URI Path starts with "/ui/static/"` → Eligible for cache, Edge TTL "Use cache-control header", Browser TTL "Respect origin".
   2. *Everything else*: `URI Path does not start with "/ui/static/"` → **Bypass cache**. The app already says `no-store`; this rule makes a misconfigured response harmless too.
4. **Never put the audio bucket behind Cloudflare.** Signed S3 links are single-use-shaped and carry PHI; caching them would hand one patient's dictation to whoever replays the URL inside the edge TTL.
5. **Rules → Transform Rules**: none needed. Do not enable "Cache Everything" or "Automatic Platform Optimization" anywhere on this zone.
6. Add the public hostname to `RADREPORT_TRUSTED_ORIGINS` if it differs from what the app sees behind the proxy, so admin writes pass the cross-site check.

## Checking it

```bash
curl -sI "https://<host>/ui/static/app.css?v=$(curl -s https://<host>/admin/login | grep -o 'app.css?v=[0-9a-f]*' | cut -d= -f2)" | grep -i -E 'cache-control|cf-cache-status'
# cache-control: public, max-age=31536000, immutable ; cf-cache-status: HIT (on the second request)
curl -sI https://<host>/admin/login | grep -i -E 'cache-control|cf-cache-status'
# cache-control: private, no-store ; cf-cache-status: BYPASS or DYNAMIC
```
