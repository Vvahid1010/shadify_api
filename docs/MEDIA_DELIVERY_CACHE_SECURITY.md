# Shadify media delivery, cache and security

## Current local-first contract (2026-10-04)

MEDIA_STORAGE_ARCHITECTURE.md defines the new local-first implementation. Its
dynamic app URLs and separate capability-only byte listener supersede the
retained direct-R2-first guidance below. The main API GuardedASGI is unchanged;
Node must narrowly route `/media/{UUID}` PUT/GET/HEAD outside the body-limited
Authentication proxy. No other API route may bypass canonical admission.

The media signing key is a separate 256-bit protected custody file, never an
Authentication/session/node credential. Capabilities bind purpose/method, UUID,
logical key, provider, MIME, exact size and expiry; private/upload grants also
bind the complete admitted identity with expiry bounded by that identity.
No email/account-ID-only assertion or first-user authority is introduced.
Wrong method/asset, malformed/tampered/expired grants and unsigned reads deny.
Local delivery rechecks current app authority or published/free/ready state;
R2 redirects are bounded by remaining capability TTL but remain provider bearer
access until expiry. No one-time semantics or immediate R2 URL revocation claim.

Both API issuance and byte responses use no-store. Byte responses also include
no-referrer and nosniff. Node/browser telemetry must not log capability queries,
R2 redirect URLs, identity evidence or secrets; Uvicorn access logging must be
disabled on the dedicated listener. No public filesystem mount, directory list,
cache rule, Worker, DNS, Cloudflare or public bucket is configured. Root is
owner-only outside source/build, with descriptor-relative no-symlink opens.
Public playback never authorizes originals/drafts or full paid tracks without
entitlement. Anonymous issuance is now the exact media GET access route, authorizing only
published free/separate preview via actual DB state, without a fabricated Auth
identity. Real Node/nginx/socket/edge proof remains separately coordinated.

## Retained R2 preparation contract

docs/R2_SIGNED_MEDIA_DELIVERY.md is the Phase 1 source of truth. This retained file
reconciles cache/security guidance with the replacement of the mandatory public
R2 bucket/Custom Domain design.

```text
Player -> shadify_api authorization -> short-lived Presigned GET -> PRIVATE R2
```

Phase 1 uses the standard R2 S3-compatible endpoint. It does not require a Worker,
public playback bucket, R2 Custom Domain or media tunnel route. media.shadify.org
is reserved for a future Worker/CDN boundary. shadify.org remains the selected VM
app host and dev.shadify.org remains WSL. Existing DNS/settings are untouched.

## Authorization and expiration

Guest free full playback requires a published free track plus its ready published
free derived delivery asset. No mandatory login is appropriate for that public
product access, even though the storage bucket is private. Paid full media needs
Authentication and existing purchase/entitlement; missing proof fails closed.
Paid previews use separate published free preview assets and cannot fall back to
the full paid object. Original masters, draft/private/unready media stay denied.
No publishing, purchase, entitlement, payment or Worker platform is added now.

The player requests fresh access by stable track_id at actual playtime and gets
only opaque url+expires_at. Playlist stores track_id; database/Track/Playlist
never persist a presigned URL. The protected playback TTL is configurable.
URLs are bearer access and reusable until expiry; same-second signing may return
the same URL. Issuing them does not create one-time use, per-request entitlement
checks or immediate revocation. No IP/HMAC IP lock is added for NAT/mobile/VPN.

## Cache and provider distinctions

API access-issuance responses must use Cache-Control:no-store. Never log URLs,
queries, credentials or bearer headers in app/gateway/player telemetry. Do not
globally cache the main application, session/API responses or signed grants.

The S3 API endpoint and public r2.dev/Custom Domain access are different surfaces.
Do not substitute a public URL or rewrite a signed S3 URL to media.shadify.org.
The Phase 1 path does not traverse an application-owned media Worker/CDN route;
do not claim application-zone WAF/rate rules or cache hits protect every S3 GET.
Cloudflare's documented presigned surface/limitations are linked in the source
of truth. Real privacy, range/CORS/expiry and request/cost behavior await proof.

Immutable/versioned derived keys remain valuable for replacement correctness and
future caching. Internal prefixes are organizational metadata, not authorization.
Storage holds durable bytes; a future CDN cache would hold temporary copies and
populate on eligible requests, not automatically when an upload completes.

## Abuse boundary and future Worker/CDN

Playback URL issuance is the future API rate-limit/abuse boundary. No rate-limit
framework is added now. Monitor storage request/cost behavior when enabled; do
not hardcode provider price/free-allocation numbers or rely solely on DDoS labels.

A future provider adapter may issue Worker access on media.shadify.org. Its
Worker could validate tokens, rate-limit abuse, check authorization per request
and read private R2 through a binding. Cache authorization must run before
protected delivery even on a cache hit. Define explicit cache lifetimes,
entitlement/revocation behavior and observability in that later reviewed phase.
Player url+expires_at, stable Playlist/Track IDs and business authorization stay
unchanged. No Worker, public origin, DRM, HLS platform or gateway is deployed now.

## Upload separation and operational proof

Existing authorized Presigned PUTs still upload directly to private R2 with
ownership/type/size/conditional-write/expiry checks. API completion uses HEAD
and a bounded GET for container identification, never full-media proxying.
PUT and GET TTL/authorization are independent. User supplies protected bucket-
scoped minimum permissions privately through storage.credentials.json later.

User/owner activation checks: approved exact frontend-origin CORS for upload and
GET/range playback, private bucket/object access, valid expiry and denied unsigned
access, authorized guest free/draft/paid behavior after real schema/HTTP wiring,
signed-URL redaction and request/cost observation. No DNS/bucket/tunnel/config,
credentials, access grants or live R2 requests are performed by this source work.


Artist management and media transfer rules have one source in
[ARTIST_PROFILES_AND_ACCESS.md](ARTIST_PROFILES_AND_ACCESS.md). Current membership/
trusted admin authorizes each private operation; uploader is audit only. New
playback issuance also requires a published non-suspended page; suspension does
not rewrite child states or revoke existing bearer URLs before their TTL.

## Future free-plan Cloudflare gateway (reviewed direction, not deployed)

The user's future direction is `media.shadify.org -> Worker Free -> Cache API
-> private R2`, using dynamic app capabilities. No Worker, DNS, bucket or paid
feature is activated here. Native S3 presigned URLs cannot be moved to a custom
domain and establish no CDN caching guarantee. See [R2 presigned URL docs](https://developers.cloudflare.com/r2/api/s3/presigned-urls/).
The retained direct SDK redirect is a future noncached adapter option, not the
selected cached Worker implementation. Local uses the same logical IDs and
opaque expiring `url+expires_at` client contract. There is no workers.dev cache
guarantee; actual host behavior needs later proof.

A future Worker must validate capability HMAC/purpose/asset/expiry before every
GET/HEAD/Range cache lookup, including hits. Automatic caching that skips Worker
authorization is unsuitable. Link TTL (e.g. 3600 seconds, already configurable)
is separate from internal immutable-object cache TTL (e.g. 24 hours); private
grants also stay bounded by their admitted identity expiry. Only after successful
authorization may the internal cache key omit the token; it must retain immutable
object identity/version and all representation selectors. Keep client responses
private,no-store and internal cache policy separate. Expiry prevents new access;
it cannot recall downloaded bytes or promise exact interruption of an in-flight
transfer. Local chunk rechecks are best-effort revocation/expiry enforcement.

Cache API is local to a data center and has no Tiered Cache via cache.put.
Store a full 200 response with Content-Length; cache.match can then return 206
for Range. cache.put rejects 206, so a range miss streams that range and does not
implicitly warm the full object. See [Cache API](https://developers.cloudflare.com/workers/runtime-apis/cache/).

As checked 2026-10-04, Workers Free permits 100,000 account requests/day, 10ms
CPU and 128MB memory; GET/HEAD/Range traffic counts, and cached object size is
limited to 512MB. These are planning limits, not constants added to application
logic. See [Workers limits](https://developers.cloudflare.com/workers/platform/limits/).
R2 Standard includes 10GB-month storage, 1 million Class A and 10 million Class B
operations monthly; egress is free, but storage/operation overages can cost money.
See [R2 pricing](https://developers.cloudflare.com/r2/pricing/). No unconditional
free-use promise or paid plan dependency is introduced.

Future activation must prove authorization before cache hits, expired-token denial
with a warm object, private/draft/paid denial, separate preview eligibility,
GET/HEAD/Range hit/miss behavior, request quotas and redaction. Provider migration
still requires explicit full-byte copy/checksum verification and per-object
cutover/rollback; an environment flag is insufficient.

### Reviewed source follow-up

Initial byte authorization precedes every response. Expiry is checked for each
streaming chunk; authority is checked before the next chunk after a fixed
two-second interval, avoiding one SQL/locking round trip per 64KiB. This is a
per-response time bound, not a shared permission cache or exact instant recall.
R2 gets an absolute grant deadline, computes a conservative TTL inside the
signing worker after queue delay, and rejects expired or deadline-overshooting
SDK URLs before returning any Location. No max(1) revival is used.
