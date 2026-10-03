# R2 signed media delivery — Phase 1 source of truth

Decision:2026-10-02. This contract replaces the earlier mandatory public R2
delivery bucket/Custom Domain design. Keep the existing architecture documents;
they now refer to this contract. No DNS, bucket, tunnel, credentials or grants
are changed by this decision or source implementation.

## Current playback path

```text
Player -> shadify_api -> short-lived Presigned GET URL -> PRIVATE R2
```

The API issues access after authoritative publication/access checks. The player
then fetches bytes directly from R2. It never downloads full media through the
API process. Phase 1 requires neither a Worker nor a public playback bucket.
Use the standard account-scoped HTTPS S3-compatible R2 endpoint.
media.shadify.org is reserved for a future Worker/CDN boundary; it is not a
Phase 1 requirement or an instruction to configure a public R2 Custom Domain.
Existing DNS is neither deleted nor reassigned by this work.

Cloudflare documents local server-side GET/PUT signing, expiry limits and the
S3 endpoint/custom-domain distinction in its
[presigned URL contract](https://developers.cloudflare.com/r2/api/s3/presigned-urls/).
URLs are bearer access: reusable until expiry, not one-time tokens. Requesting
access again invokes the signer again; identical key/TTL/signing-second inputs
can produce the same URL. No uniqueness promise or replay ledger is added for
R2 media URLs. The native shell's ON replay control is a separate API mechanism.

## Stable data and opaque player access

PostgreSQL stores asset metadata, stable asset/track IDs and the storage key.
For example, an internal immutable derived key may be:

```text
delivery/artists/{artist_id}/tracks/{track_id}/{asset_id}/audio.m4a
```

The key is backend storage metadata, never the Player contract. Track and
Playlist persist no presigned URL; a Playlist keeps stable track_id references.
A playlist saved for years remains useful because each actual play requests
fresh access by track_id, rather than persisting an expiring URL.

Prepared access response:

```json
{"url":"<opaque-short-lived-access-url>","expires_at":"<UTC-ISO8601-expiry>"}
```

The frontend passes the opaque URL to its player. It neither constructs nor
parses account IDs, bucket names, keys, signing parameters or credentials. It
refreshes access after expiry/failure and rechecks current authorization. A
returned URL necessarily contains provider-specific information internally;
no separate provider fields are part of the domain response. The URL and query
must stay out of logs, analytics, persistent browser storage and database rows.
Access-issuance responses use Cache-Control:no-store when HTTP wiring exists.

## Authorization boundary

| Requested media | Required proof before signing |
| --- | --- |
| Free full track | Authoritative published non-suspended artist page, published free track and ready published free derived delivery asset; guest allowed without mandatory login. |
| Paid full track | Authentication plus existing purchase/entitlement; deny if that proof is unavailable. |
| Paid preview | Published non-suspended page, published track and its separately selected ready published free preview asset; guest allowed. Never fall back to the full paid object. |
| Original/draft/unready/private/unknown asset | Deny; upload completion is not publication or playback authorization. |

The artist mission schema can represent publication/derived eligibility but
provides no processing output; originals still enforce private/draft constraints.
See [ARTIST_PROFILES_AND_ACCESS.md](ARTIST_PROFILES_AND_ACCESS.md) for current
membership authority, transfer-safe media and page visibility. PlaybackService is a
prepared read-only boundary with a PlaybackRepository protocol; the live
Postgres repository does not implement that projection and no playback HTTP
route is registered. It checks authoritative page, track and asset publication (missing page status
fails closed),
readiness, derived/preview type and free access before calling the provider.
An authenticated account alone never authorizes paid full playback. Purchase,
entitlement and payment execution remain unavailable. Artist authority/publication
APIs are implemented in source, without operational grants or guest enrollment.

When public/free HTTP wiring is approved, it must accept guests and load
publication/free status server-side. Do not route it through a mandatory-login
dependency or accept browser-provided published/free/key/entitlement fields.
Keep existing Authentication independent; coordinate any ingress/catalog change
with its owner. No guest public route or shared Auth/topology change occurs here.

## Existing provider and protected configuration

R2Storage continues to implement ObjectStorage upload/inspection. It also
implements MediaDeliveryProvider.create_playback_access(key), returning
PlaybackAccess(url,expires_at). PlaybackService depends on this protocol, not
boto3, S3 parameters, hostname parsing or cryptography. GET and PUT reuse the
existing SDK/client configuration but have independent domain authorization.
Only the server selects keys; the signer itself does not assert publication,
existence, CORS, entitlement or real cloud reachability.

Use the existing protected storage.credentials.json in the runtime owner's
confirmed CREDENTIALS_DIRECTORY. Absolute deployed directory/protected UI route
is still owner-assigned. Never place values in chat, Git, frontend bundles,
another app's files or logs. Example placeholders, not usable credentials:

```json
{"provider":"r2",
 "endpoint_url":"https://<32-character-account-id>.r2.cloudflarestorage.com",
 "bucket":"<assigned-existing-private-bucket>",
 "access_key_id":"<privately-supplied-access-key-id>",
 "secret_access_key":"<privately-supplied-secret-access-key>",
 "upload_ttl_seconds":300,"playback_url_ttl_seconds":900,
 "max_upload_bytes":104857600}
```

playback_url_ttl_seconds is a protected backend setting, default 900 seconds,
validated as an integer in the R2 signing range1..604800. Keep it short for the
actual access policy;900 is an example/default, not a hardcoded track/domain rule.
Older storage files without the field remain valid in the updated reader. The
deployed 4ec0b0d reader rejects unknown fields; do not add this optional setting
to that baseline projection before selecting a compatible updated package. GET signing records a
conservative UTC expiry before SDK signing; errors and repr omit the signed URL.
The existing bucket can hold private originals and derived namespaces; a separate
public bucket is unnecessary. If storage allocations later separate classes,
preserve the same provider/domain boundary rather than expose bucket choices.

Use minimum bucket-scoped permissions needed by enabled server operations.
Playback needs object read; current uploads also need PUT and bounded HEAD/GET
inspection. Reuse the protected configuration; no credentials are generated,
read from an operational file, printed, transmitted or expanded by this task.
Bucket public access remains off. The user supplies real values privately later.

## Uploads, expiry, network changes and abuse

Authorized Presigned PUT uploads remain direct to private R2 with existing
ownership/type/size/conditional-write/expiry checks. Large bytes never traverse
the API. Multipart/resume stays a later video-upload requirement. Upload URL
TTL and playback URL TTL are independent. Original replacement uses new keys.

No IP-bound token, HMAC IP lock or client-IP policy is introduced. NAT, mobile
networks and VPN changes must not invalidate an otherwise valid media URL.
Do not claim immediate revocation or per-request entitlement checking for an
already issued R2 URL; a URL remains usable until expiry under its storage policy.

The future playback URL issuance endpoint is the API rate-limit/abuse boundary.
No rate-limit framework is added now. Do not claim application-zone WAF/CDN
rules protect or cache every direct R2 S3 GET. Inspect actual browser CORS,
range requests, expiry, object privacy and storage requests/costs when enabled.
No global application Cache Everything rule is appropriate.

## Future Worker adapter

A later MediaDeliveryProvider can issue Worker access on media.shadify.org.
That Worker could validate purpose-specific tokens, enforce rate/abuse limits,
check authorization per request and read R2 through a private binding. Cache
authorization and cache lifetime would need a separate reviewed policy.
Player still receives url+expires_at; Playlist still stores track_id; Track and
business authorization still use stable identities. This is a future adapter,
not a new gateway/framework, current custom-domain/public-bucket instruction,
Worker creation/deployment, DRM, HLS platform or payment implementation.

## Evidence and remaining real proof

Focused offline tests use only explicitly synthetic fixtures and the real SDK
signer with HTTP/socket access blocked. They cover GET method/key/configured
TTL/expiry/redaction, guest free access, paid denial, separate previews and
draft/original denial. Existing direct PUT tests remain required.
Local signing does not prove bucket access, uploaded bytes, real expiry, CORS,
range behavior, publication, entitlement or a live player. Current reviewed
native package/source4ec0b0d remains the deployment baseline unless a compatible
updated package is intentionally published. No live effect is implied by the
source/provider preparation.

Next live checks, after owner-approved assignments and schema/route wiring:
user privately supplies R2 credentials; prove one tiny supported PUT/completion,
private derived object GET/range/CORS/expiry, guest free publication checks,
draft/original denial and paid-full denial without entitlement. Do not fabricate
publication or purchase records merely to report successful playback.


Source verification for this preparation:51 offline tests pass and pip check is
clean. These include the retained direct PUT/inspection/ownership/runtime/native
replay tests and7 new signed GET/publication/access-policy tests. SDK network
calls are blocked and policy inputs are explicitly synthetic. No deployment
package replacement or live playback acceptance is asserted by this source push.
