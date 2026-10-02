# Shadify media delivery, cache and security

## Current contract

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
