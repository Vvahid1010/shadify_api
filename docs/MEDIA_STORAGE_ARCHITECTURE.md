# Shadify Media Storage Architecture

> Media delivery, CDN cache, the dedicated media hostname, protected-media access and DDoS/abuse cost controls are defined in `docs/MEDIA_DELIVERY_CACHE_SECURITY.md`.

## Decision

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

Development and production must eventually use separate durable namespaces, preferably separate buckets.

## Originals and delivery

Original masters are private source assets such as WAV, FLAC, original uploaded video, and original artwork.

Originals are immutable-by-key. Replacing a master creates a new asset/version rather than overwriting the existing object.

Normal playback uses derived delivery assets, not the original master.

Initial delivery direction:

- Audio: AAC/M4A and practical quality variants; HLS may be introduced for adaptive/controlled streaming.
- Video: HLS adaptive streaming with only the resolutions supported by the source, for example 1080p, 720p, 480p and 360p.
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

This is conceptual, not a frozen schema.

The database is authoritative for ownership, domain relationships, processing/publish state and access/entitlement decisions. Object storage is authoritative for object bytes.

## Free, paid and preview media

Free/public delivery can be CDN/cache friendly.

Paid/private full media must not use a permanent public object URL as an entitlement mechanism.

Future paid flow:

    Fan requests full media
        -> shadify_api checks Authentication and entitlement
        -> short-lived controlled media authorization
        -> CDN/object storage
        -> Shadify Player

Chapary will handle future payment execution. Shadify API remains authoritative for resulting entitlement state.

Paid releases should use a separate derived preview asset. Preview access must never be implemented by exposing the unrestricted full paid object.

Detailed delivery/cache/security behavior is defined in docs/MEDIA_DELIVERY_CACHE_SECURITY.md.

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
