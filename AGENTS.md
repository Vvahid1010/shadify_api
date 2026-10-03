# Shadify API Repository Guidance

Use main by default unless the operator explicitly requests another branch.

## Repository role

shadify_api will become the server-authoritative backend for Shadify. The repository is still architecture-first. Do not scaffold the API, database, media worker, payments, or production deployment unless that implementation phase is explicitly requested.

Before any media/storage work, read:

- docs/R2_SIGNED_MEDIA_DELIVERY.md (Phase 1 source of truth)
- docs/MEDIA_STORAGE_ARCHITECTURE.md
- docs/MEDIA_DELIVERY_CACHE_SECURITY.md

## Core boundaries

- Reuse the existing central Authentication service. Do not create a second Shadify login system.
- Shadify API is authoritative for artist ownership, content ownership, publish state, future entitlements, and support/purchase relationships.
- Future Chapary integration performs payment execution; Shadify remains authoritative for what artist/content the payment relates to and the resulting entitlement state.

## Media storage contract

Initial provider: Cloudflare R2 Standard.

Development and early testing should stay within the available R2 Standard free allocation where practical. This is a provider choice, not an architectural lock-in.

Mandatory rules:

- Never store music/video binaries in PostgreSQL.
- Never use GitHub as media storage.
- Never use application-node disks as durable media storage.
- Node Agent/application nodes remain stateless with respect to durable media.
- Browser uploads large media directly to object storage through short-lived server-authorized upload sessions/URLs.
- shadify_api validates identity, artist ownership, asset intent and metadata; it should not proxy whole large upload bodies.
- Original masters are private and immutable-by-key. Replacements create a new object/version.
- Delivery assets, previews, thumbnails and artwork variants are separate from originals.
- Database rows store metadata and storage keys/asset IDs, not large binary media.
- Paid/private media must not depend on permanent public object URLs.
- Large video uploads should support multipart/resumable upload when implemented.
- Storage access must remain behind a small provider abstraction so cold media can later move to R2 Infrequent Access or another compatible provider without redesigning the product.

## Media delivery contract

Phase 1: Player -> shadify_api -> short-lived Presigned GET URL -> private R2.
No Worker or public playback bucket is required. media.shadify.org is reserved
for a future Worker/CDN boundary, not a current public R2 Custom Domain mandate.
Do not delete/reassign DNS or change Cloudflare settings from this source task.
See docs/R2_SIGNED_MEDIA_DELIVERY.md for the authoritative current contract.

Guest playback must work for published free tracks after authoritative checks.
Paid full media requires Authentication and existing purchase/entitlement; absent
proof denies. A free paid preview uses a separate asset. Do not implement payments
or infer entitlement. Current schema remains draft/original-only; do not expose
playback HTTP routes without approved publication/repository/ingress wiring.
Player receives opaque url+expires_at; Playlist stores stable track_id. Database,
Track and Playlist never persist presigned URLs. Keys remain backend metadata.
URLs are reusable until expiry; same-second signing may return identical URLs.
No IP binding/HMAC IP lock. TTL comes from protected storage configuration.

Keep the existing object-storage/provider abstraction; domain code must not use
boto/S3 signing parameters. Future Worker delivery replaces its provider adapter.
Cache/authorization and API issuance rate controls remain separate concerns;
no global application caching or speculative rate-limit framework is authorized.

## Initial simplicity policy

For the first backend/media phase:

- use R2 Standard for originals, delivery, previews and artwork;
- keep the development bucket private by default;
- use server-generated IDs in object keys, never trust user filenames as authoritative keys;
- do not add another provider or storage tier before real usage justifies it;
- do not hard-code provider pricing/quota numbers into application logic;
- do not enable broad "cache everything" behavior on the main Shadify application hostname;
- keep media security and cache policy scoped to the dedicated media delivery layer.

The first likely future optimization is hot delivery on R2 Standard and cold originals on R2 Infrequent Access or another compatible object store.

## Security

When implementation begins:

- keep storage credentials server-side;
- use short-lived upload/access authorization;
- validate MIME/container and configured size limits server-side;
- never trust browser-supplied ownership or storage keys;
- keep originals private;
- never log signed URLs or secrets;
- explicitly track upload, processing and publish state;
- prepare the access-issuance endpoint as the future abuse/rate-limit boundary;
- do not claim application-zone WAF/CDN rules cover direct R2 S3 playback;
- treat cache and authorization as separate concerns;
- do not assume every abusive request will be classified as DDoS; design rate limits and cache policy to reduce billable origin/R2 reads.

## Runtime

Follow the native Shadify runtime policy when implementation begins:

- native Linux services managed by Node Agent + systemd;
- no Docker/Compose;
- same-origin browser API routing through the approved ingress/proxy;
- durable media remains outside application nodes.

If the media storage contract changes, update docs/MEDIA_STORAGE_ARCHITECTURE.md in the same change.
If media hostname, cache, access or abuse-protection behavior changes, update docs/MEDIA_DELIVERY_CACHE_SECURITY.md in the same change.


## Current API implementation (2026-10-02)

The operator authorized authenticated API/PostgreSQL/R2 integration and scoped
normal publication. Source is /srv/Coding_space/shadify_api on Ubuntu WSL ext4;
use native Python 3.14 and the isolated source-local .venv. No managed WSL API
service, database, migration or cloud resource has been activated.

Current scope: canonical shell-admitted Authentication identity, Shadify user
projection, already-owned artist draft tracks, private original upload issuance/
completion and metadata. Artist eligibility/enrollment remains undefined; do not
infer artist ownership or create grants from a browser or Authentication role.
Phase 1 signed GET/config/provider and read-only authorization-boundary preparation
is authorized. Playback HTTP/publication schema, purchases/entitlements/payments
and Worker deployment remain pending; keep the existing private API scope.

Node Agent owns installation/update/lifecycle on selected Node B, catalog
shadify-api, app identity shadify_api. It owns protected config/profile and native
transport/connection/checkpoint/replay projections. Do not modify Node Agent,
shared WSL topology or Authentication independently. Package only the existing
Python3.14 Linux x86_64 source/offline-wheel/hash-manifest shape; source and build
publication use normal non-force pushes scoped to this repository.

shadify.org is the accepted VM app hostname; dev.shadify.org remains WSL.
media.shadify.org is reserved for a future Worker/CDN delivery boundary. Phase 1
uses private R2 S3 presigned GETs; no public playback bucket/Custom Domain is
required. Originals/drafts and derived delivery remain private. User owns Cloudflare/R2 setup and supplies keys through the
confirmed protected runtime route; do not request values in chat or read them.

See local_bootstrap.md and docs/API_FOUNDATION.md for app-owned exact filenames,
endpoints, validation and remaining real Auth/DB/R2/VM proof. The canonical shell
authenticates complete admitted requests; this app has no standalone HMAC crypto
fork. Canonical shell admission is the accepted identity trust boundary; no independent
assertion HMAC verifier is required.

## Artist profiles and access mission (2026-10-03)

The operator authorized staged artist-page backend implementation. Read
docs/ARTIST_PROFILES_AND_ACCESS.md before artist, content or media permission work;
it is the single contract and implementation-status source. Stage 1 requires the
parent's independent Astra acceptance before data/authorization or API changes.
Reuse canonical Authentication and per-page memberships; never infer is_artist,
admin or durable media access from browser fields or uploader IDs. Ownership,
publication and verification stay independent. Isolated database tests/migration
scripts are allowed; live migrations, grants, cloud effects and shared topology
changes require separately coordinated authority. Frontend work is limited to
documentation references; the other owner retains UI/session integration.
