# Shadify API Repository Guidance

Use main by default unless the operator explicitly requests another branch.

## Repository role

shadify_api will become the server-authoritative backend for Shadify. The repository is still architecture-first. Do not scaffold the API, database, media worker, payments, or production deployment unless that implementation phase is explicitly requested.

Before any media/storage work, read:

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

Media delivery must be isolated from the main application host.

The intended public media hostname is:

- media.shadify.org

Do not use the application hostname as the long-term media delivery surface.

Reasons:

- keep application HTML/session/API cache rules separate from media cache rules;
- allow aggressive media caching without caching the whole application;
- apply media-specific WAF, rate limiting and abuse controls;
- keep public/free delivery distinct from protected paid delivery;
- reduce unnecessary origin/R2 reads through CDN caching;
- allow future Worker-based authorization for protected media without changing the app routes.

Cache is populated on request, not at upload time.

For public/free delivery, the intended request path is:

Internet -> Cloudflare DDoS/WAF/Rate Limit -> CDN Cache -> R2 only on cache miss.

For paid/private full media, authorization is server-controlled and short-lived. A permanent public media URL must never act as proof of entitlement.

See docs/MEDIA_DELIVERY_CACHE_SECURITY.md for the full contract.

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
- use Cloudflare DDoS protection plus media-specific WAF/rate-limit rules before R2;
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


## Implemented source foundation (2026-10-01)

The operator explicitly authorized initial API/R2 preparation. Source lives at
`/srv/Coding_space/shadify_api` on Ubuntu ext4; use WSL-native Python >=3.12.
No managed local API venv, unit, listener or database is installed. Source-only
status grants no ad hoc runtime activation. See `local_bootstrap.md` and
`docs/API_FOUNDATION.md` for tests, exact protected configuration and the proposed
distinct Authentication proxy identity. Default uploads fail closed; no real
R2, database, media delivery, processing or entitlement proof exists yet.
Coordinate shared topology/Auth changes with their owner; do not enroll
Shadify as Mizfood or copy shared crypto/framework code.
