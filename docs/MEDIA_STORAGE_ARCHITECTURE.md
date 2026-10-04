# Shadify Media Storage Architecture

> Historical R2 preparation reference: `docs/R2_SIGNED_MEDIA_DELIVERY.md`. Cache and future Worker/CDN policy: `docs/MEDIA_DELIVERY_CACHE_SECURITY.md`. The earlier mandatory public R2 playback/Custom Domain design is replaced.

## Current local-first implementation contract (2026-10-04)

The user selected local durable media now, with dynamic signed expiring links
and a future R2 migration using the same client and business contracts. This
section supersedes the retained earlier R2-first decision below. Local storage
is an object adapter, not an S3 emulator. No server or cloud resource was deployed.

`StorageService` owns app media capabilities; `LocalAdapter` and `R2Storage`
implement the actual byte/provider operations. The default provider is selected
by protected `storage.credentials.json` (`provider: local|r2`); optional
`STORAGE_PROVIDER` must match that explicit config, never silently fall back.
Local mode never loads an R2 client or R2 credentials. Local reads continue to
work after changing the default to R2 when its protected config also retains
the existing `local_root`. Each row's provider selects the adapter; changing the
default affects only new assets. Missing adapters fail closed.

DB rows retain logical key, asset UUID and provider. New original keys use
`artists/{artist_id}/tracks/{track_id}/{asset_id}/master`. Derived keys may use
the same immutable asset prefix, artwork may use `covers/{asset_id}/...`, and
valid historical originals/delivery/previews keys continue to work. No URL,
physical root, signing key or file bytes are persisted in PostgreSQL. Replacing
a master creates a new UUID/key. No applied migration is modified or added.

API issuance preserves `upload:{method,url,headers,expires_in}` and returns
read access as `{url,expires_at}`. URLs use the app origin and `/media/{asset_id}`
with a signed temporary capability; clients never choose keys/providers/roots.
They are reusable until expiry, not permanent public or one-time links.
POST `/api/media/{asset_id}/access` requires current content-management authority.
POST `/api/tracks/{track_id}/playback?preview=false` uses the actual DB projection:
page/track/asset must be published, asset ready and appropriately derived/free.
Originals, drafts, suspended pages and full paid tracks without entitlement deny.
Paid previews are independent free assets. No processing or implicit publishing
is introduced. Source fixtures deliberately create ready derived rows only for
testing; no runtime producer exists. Normal upload completion remains private/draft.

The separate `shadify_api.media_runtime:app_factory` listener exposes only
PUT/GET/HEAD `/media/{UUID}` and GET `/media/v1/tracks/{UUID}/access`.
The agreed Node-owned Unix socket is `/run/node-agent-shadify-media/receiver.sock`,
with a small Shadify unit under the same API UID and only DB/storage/media-signing
custody (no canonical transport keys). Node routes these exact paths/methods,
never `/api`; it must disable/redact URL query and
authorization logs and apply its existing request/timeout/disk controls. All
business/issuance routes remain on canonical GuardedASGI (catalog 56 routes,
Authentication peer only). Large media bodies do not traverse Authentication.
Anonymous GET `/media/v1/tracks/{UUID}/access` on the same media handler issues
only real-DB-authorized published free full/separate free preview access. Its only
optional query is `preview=true|false`. It creates no Authentication identity and
cannot authorize originals/drafts/private/unentitled paid full. Private access,
upload creation/completion and the canonical API issuance remain Auth-guarded.

Local root is explicitly protected, runtime-owned, owner-only, absolute, outside
source/build and already present; startup never provisions it. Linux xattrs
preserve MIME metadata. Every path component is opened without symlink following;
keys reject traversal. Private temporary files stream bounded bytes, require exact
length/MIME/immutable headers, optionally validate `x-amz-checksum-sha256`, and
are fsynced then exclusively atomically linked into the final key. This provides
the no-overwrite publication invariant of an exclusive rename without relying
on Linux-only renameat2. Failed/truncated/disk-full uploads remove temporary files.
No full upload buffering, S3 server or extra dependency/framework is added.

Before final local file publication, existing artist/admin/identity/asset locks
revalidate current authority, pending state and expiry. Identity expiry or audit
failure removes a newly installed file. Completion retains existing bounded
inspection, container validation, locked transition and final identity checks.
Local GET/HEAD supports single bounded/open/suffix byte ranges, 206/416 and
Content-Range/Accept-Ranges; reads recheck current authority/publication and TTL,
with expiry per chunk and authority checks before the next chunk after a fixed two-second per-response interval. Initial authorization precedes all bytes; there is no shared permission cache. Initial denial precedes bytes; expiry or
revocation during a response truncates it against its declared Content-Length.
SHA256 can be computed locally for migration verification. This is parity with
the supported R2 contract, not full S3 emulation; multi-range, resumable video,
ETag conditions, transcoding and entitlement remain outside this slice.

R2 remains optional and disabled in local mode. A capability-valid request uses
307 to a private SDK-presigned PUT/GET/HEAD bounded by an absolute capability deadline, recomputed inside the signing worker and checked against the SDK signature timestamp; expired grants never revive with a one-second fallback;
PUT retains size/MIME/If-None-Match and optional SHA256. Browser replay/Range
within TTL remains supported. After redirect, R2 bearer access lasts until expiry;
application revocation cannot recall an already issued provider URL. Real R2
CORS/privacy/expiry/range/conditional write/checksum behavior remains unproved.

### Future migration and rollback

1. Explicitly configure the future private R2 destination while retaining local
   storage and the app URL/signing contract; do not change existing row providers.
2. Copy immutable keys, preserving MIME and size. Verify complete SHA256 byte
   equality and destination metadata/private access before selecting each object.
3. Atomically update only each verified object's provider, preserving UUID/key/
   references/publication. Existing issued capabilities bind the original provider
   and fail closed after a change; request fresh access at the same app URL path.
4. Change the new-upload default independently. Keep local originals during the
   reviewed rollback window; rollback provider pointers only when matching bytes
   still exist. Never point R2-only new objects at absent local files.

No automatic migration/copy/delete tool or real R2 validation is implemented.
`STORAGE_PROVIDER=r2` alone does not copy media. WSL and B roots/data remain
separate; disk sizing and installation belong to Node ownership.

## Retained earlier decision

Shadify media uses object storage. The initial provider is Cloudflare R2 Standard.

During development and early testing, the goal is to remain inside the available R2 Standard free allocation where practical. We deliberately avoid adding storage tiers or providers before real usage exists.

R2 is the initial provider, not a permanent architectural dependency.

## Core model

Shadify application nodes manage product logic. Object storage owns media bytes. PostgreSQL stores metadata, ownership and state.

Never store music or video binaries in PostgreSQL, GitHub, or durable local disks on Node Agent-managed application servers.

This keeps application nodes stateless and avoids media replication when Shadify later adds more nodes or regions.

## Initial storage layout

Use R2 Standard for all media classes during the first phase:

    originals/
    delivery/
    previews/
    artwork/

Use stable server-generated IDs in keys rather than user filenames.

A conceptual layout is:

    originals/artists/{artist_id}/releases/{release_id}/tracks/{track_id}/{asset_id}/master
    delivery/artists/{artist_id}/releases/{release_id}/tracks/{track_id}/{asset_id}/...
    previews/tracks/{track_id}/{asset_id}/...
    artwork/artists/{artist_id}/...
    artwork/releases/{release_id}/...
    artwork/users/{user_id}/...
    artwork/moments/{moment_id}/...

Development and production use separate durable namespaces, preferably separate buckets.

Phase 1 originals and derived delivery remain private. The player obtains an
API-authorized short-lived Presigned GET URL for a stable server-selected key and
fetches R2 directly through the standard S3-compatible endpoint. No public playback
bucket or Worker is required. Private namespaces can use the assigned existing
bucket; separate allocations may be chosen later without changing the Player.
media.shadify.org is reserved for a future Worker/CDN boundary, not a current R2
Custom Domain/public-bucket instruction. No bucket/domain/DNS change occurs here.

## Originals and delivery

Original masters are private source assets such as WAV, FLAC, original uploaded video, and original artwork.

Originals are immutable-by-key. Replacing a master creates a new asset/version rather than overwriting the existing object.

Normal playback uses derived delivery assets, not the original master.

Initial delivery direction:

- Audio: AAC/M4A and practical quality variants; HLS may be introduced for adaptive/controlled streaming.
- Video: source-appropriate derived delivery; HLS/adaptive variants are deferred to an approved processing/player phase, not a Phase 1 platform requirement.
- Artwork: preserve the original and generate optimized responsive variants such as AVIF.

The exact codec/bitrate matrix is intentionally deferred until the media-processing phase.

## Upload flow

Large uploads should not pass through the Shadify API process.

Target flow:

    Artist browser
        -> requests upload from shadify_api
        -> shadify_api validates Authentication identity, artist ownership and asset intent
        -> shadify_api returns short-lived upload authorization
        -> browser uploads directly to R2
        -> shadify_api verifies the uploaded object
        -> MediaAsset becomes uploaded / pending processing

For large videos, use multipart/resumable upload when implemented so an interrupted connection does not restart the entire file.

The browser never receives storage credentials.

## Media processing

A future media-processing worker will read private originals and produce:

- playback variants;
- HLS manifests/segments;
- previews;
- thumbnails/posters;
- optimized artwork.

Track explicit states such as upload_pending, uploaded, processing, ready and failed.

Do not choose or build a complex transcoding platform during the current documentation/frontend phase.

## Database boundary

PostgreSQL stores metadata and relationships, never large binary objects.

A future MediaAsset model may include concepts such as:

- id;
- owner_user_id / artist_id;
- track_id, release_id or moment_id;
- asset_type;
- storage_provider;
- storage_key;
- original_asset_id;
- mime/container/codec metadata;
- size and duration;
- processing status;
- access class;
- version.

This is conceptual, not a frozen schema. Presigned URLs are never persisted in
PostgreSQL, Track or Playlist; Playlist retains stable track_id and requests new
url+expires_at at actual playtime. Stable object keys remain backend metadata.

The database is authoritative for ownership, domain relationships, processing/publish state and access/entitlement decisions. Object storage is authoritative for object bytes.

## Free, paid and preview media

Published free tracks allow guests without mandatory login, but storage remains
private. The API checks authoritative published+free status and ready derived
assets before signing GET access. Current draft originals are never playable.

Paid/private full media must not use a permanent public object URL as an entitlement mechanism.

Future paid flow:

    Fan requests full media
        -> shadify_api checks Authentication and entitlement
        -> short-lived controlled media authorization
        -> private R2 through short-lived Presigned GET (Phase 1)
        -> Shadify Player

Chapary will handle future payment execution. Shadify API remains authoritative for resulting entitlement state.

Paid releases should use a separate derived preview asset. Preview access must never be implemented by exposing the unrestricted full paid object.

Full paid playback remains closed when existing entitlement proof is absent; no
purchase/entitlement/payment implementation is introduced. Free paid previews
are separate assets, never an unrestricted full-object fallback. Current delivery
and response contracts are in docs/R2_SIGNED_MEDIA_DELIVERY.md.

## Provider abstraction

Storage-facing backend code should use a small object-storage interface for operations such as:

- create upload authorization/session;
- verify/complete upload;
- read object metadata;
- create controlled delivery access;
- delete/archive;
- copy/migrate when required.

Do not leak R2-specific details throughout domain services or frontend contracts.

Future compatible providers may include R2 Infrequent Access, Backblaze B2, Google Cloud Storage, AWS S3, or another object store. Do not implement these adapters until needed.

## Cost evolution

### Initial stage

Use only R2 Standard and aim to stay within its free allocation during development.

Do not hard-code current provider pricing/quota numbers into application logic or architecture decisions.

### Growth stage

When real storage and traffic justify optimization:

    hot delivery / previews / frequently used artwork
        -> R2 Standard

    cold original masters
        -> R2 Infrequent Access
           or another compatible provider if materially better

Choose based on total cost, including storage, retrieval, egress, requests, reprocessing needs and operational complexity — not storage price alone.

### Larger scale

A future hybrid can become:

    Original masters -> lower-cost compatible storage
    Hot delivery     -> R2/CDN
    Metadata         -> PostgreSQL
    Authorization    -> shadify_api

That migration must not require redesigning Shadify pages, Artist Studio, Player or domain models.

## Security invariants

When implementation begins:

- storage credentials remain server-side;
- signed upload/access authorization is short-lived;
- the API selects final object keys;
- upload intent is bound to authenticated owner + asset type;
- validate MIME/container/signature rather than trusting extensions;
- enforce configured size/type limits;
- originals remain private;
- do not log signed URLs or secrets;
- reject attempts to attach objects to another user's/artist's domain records;
- clean abandoned uploads through a later lifecycle policy.

## Explicit non-goals now

Do not implement yet:

- R2 Infrequent Access;
- multi-provider replication;
- media transcoding infrastructure;
- DRM;
- paid-media entitlement;
- Chapary integration;
- automatic lifecycle/cold-tier jobs;
- cross-region replication;
- custom CDN/media microservices.

The invariant is simple:

Shadify nodes manage product logic; object storage owns media bytes.

Start with R2 Standard and its free allocation. Optimize only after real usage data exists.


Artist management and media transfer rules have one source in
[ARTIST_PROFILES_AND_ACCESS.md](ARTIST_PROFILES_AND_ACCESS.md). Current membership/
trusted admin authorizes each private operation; uploader is audit only. New
playback issuance also requires a published non-suspended page; suspension does
not rewrite child states or revoke existing bearer URLs before their TTL.

The future cached delivery direction is a capability-validating Worker Free on
media.shadify.org with private R2, not native S3 presigned URLs on a custom domain.
See MEDIA_DELIVERY_CACHE_SECURITY.md for checked limits, link/cache TTL separation
and validation-before-cache invariants. No Worker or cloud activation is included.


## Approved Local signed-link delta (2026-10-04)

LocalStorageConfig defaults playback/private-access grants to300seconds and
rejects configured values above300. StorageService additionally caps issuance by
the recorded object's local provider, even if the default provider changes or a
caller supplies an older900-second setting. A shorter admitted actor/session
expiry still wins. Upload lifetime retains its existing300-second default.
R2 implementation/qualification is suspended; its completed dormant source and
storage abstraction stay intact, with no cloud credential or activation.

Public guest issuance reuses GET /media/v1/tracks/{track UUID}/access with optional
preview=true|false. Private issuance and upload/completion remain Auth-admitted.
Every new issuance/renewal rechecks current policy; there is no refresh token or
cached grant. Stable /media/{asset UUID}, logical keys and per-object provider are
preserved; only the transient signed query and expires_at change. Development
origin is https://dev.shadify.org, selected by protected Node settings. Node must
pair issuer origin with the exact selected website/nginx Host; the private byte
factory itself does not add a competing Host-policy implementation.

Streaming checks expiry after an awaited authority recheck and again after the
awaited filesystem read immediately before yielding. A queued read cannot extend
the grant. Authority checks retain the existing two-second per-response interval.
Expiry/revocation ends a response with its declared Content-Length incomplete;
previously delivered/browser-buffered bytes cannot be recalled. Private/public
issuance, successful bytes, Range and errors use private,no-store. No capability
URL belongs in catalog rows, SSR output, logs or durable browser storage.

The delta is isolated from nomination source in
/tmp/shadify-local-media-300-20261004, derived from media-only473ff8f718b4cad5e9a86e3a13bdab8879de125b
with exact qualified runtime63e19536. Nomination008/catalog58 are absent. Focused
tests cover Local300 default/upper bound, dev URL/path, shorter actor expiry,
renewal paid/suspension/owner-transfer denial and expiry after a delayed read.
Existing upload/Range/Unix evidence is reused where unchanged. Synthetic derived
fixtures are explicitly seeded, not a processing/publishing feature.

Actual Node-generated dev Host pairing, decodable browser playback/renewal and
the selected qualified Node/API/Web tuple remain coordination gates. Node B
installation was explicitly authorized by the operator, but this API source/test
work does not claim B installation or acceptance. Hyper-V inventory access is
denied; required persistent credential/access decisions remain specific owner
blockers. No tunnel/network/security change, real grant, live root or credential
is created here. Old frozen63/7a/9a artifacts remain unchanged.


Exact media-only source 94143222de5c249a63e62588ff6b6d124e5d8acd was frozen at
.build-tools/local-300-qualification/shadify-api-local-300-94143222.tar.gz.
Archive SHA256 76fe071a75a22c417ed43ae897ffc4173e3fb53e13315c5773426b176b8c2615;
manifest SHA256 0f72daf2e966c98dc4eeca59817548d56d60882d7ec8c55fc6e5db503811a3ae.
Fresh offline hash-pinned production/test installs and both pip checks passed.
All53 packaged targeted tests passed without skips:21 Local/config cases,
one actual nginx/Unix factory test,7 artifact verification,7 identity and17
ordinary artist/API cases. Packaged source/SQL bytes match the exact commit;
only config.py,main.py,media_routes.py,storage_service.py differ from media63.
Schema006,catalog56,wheels/dependencies/signing format/logical paths remain
unchanged. R2-specific qualification was deliberately excluded. Receipt/logs
are in the owning qualification directory. Actual Node-generated dev routing,
decodable browser renewal and B installed/runtime proof are not asserted.
