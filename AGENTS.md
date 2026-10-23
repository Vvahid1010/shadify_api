# Shadify API Repository Guidance

## Shared DEV policy

For this operator's DEV work, read and apply the
[shared DEV simplicity policy](../node_agent/AGENTS.md#dev-simplicity-policy---operator-decision-2026-10-10).
It preserves existing security and runtime authorization boundaries.

Use main by default unless the operator explicitly requests another branch.

## Agreed scope policy

- State the current user outcome and a concrete done criterion before work.
  If no goal is agreed, do not invent one.
- Reuse the existing proven application or workflow pattern first.
- Fix only direct blockers to the agreed outcome. Park unrelated improvements
  separately; do not implement them as prerequisites.
- Before material changes to architecture, infrastructure, dependencies,
  security policy or agreed scope, explain the concrete need and smallest
  change, then coordinate with the user before implementation.
- Continue ordinary work within the agreed goal autonomously. Routine bug fixes
  and minor dependency updates do not require another approval by default.
- Preserve existing security requirements and defaults; never weaken them for
  convenience. Existing runtime and deployment authorization still applies.
- Carry the outcome, done criterion, scope/exclusions, owner boundaries and
  unresolved blockers into delegated tasks and handoffs.
- Report the actual usable result and remaining blockers with evidence.
  Passing tests alone does not establish that the requested flow works.

## Current Shadify outcome — dev WSL

The agreed goal is actual upload, ingest and playback plus a sample artist
profile in dev WSL, reusing the proven central Authentication pattern. This
defines the current outcome over historical phase descriptions; it does not
claim completion or grant new runtime, credential or deployment authority.

Done means the authorized dev WSL flow demonstrates a real upload through
the existing server approval/security gates, ingested content that plays, and
the sample artist profile using central Authentication. Report missing proof
as a blocker; fixtures and passing tests alone are insufficient.
Node Agent remains only the execution platform. Do not add shared Topology
redesign or generic fault-harness qualification gates to this goal; coordinate
direct blockers with the owning project.

## Canonical music ingest/playback contract (2026-10-06)

Read docs/SHADIFY_MUSIC_INGEST_PLAYBACK.md before upload, media, matching,
processing or playback work. It is the canonical eleven-section user contract
and supersedes conflicting historical media readiness/storage/delivery guidance
below. ALL uploads, including artist/admin/single/batch/PC/Android, are untrusted:
quarantine -> mandatory open-source server malware scan -> validation -> extraction
-> metadata-only MusicBrainz matching/review -> ingest-once processing -> approval.
No preview, playback, usable file delivery or file publication before server approval;
uploaded is not ready. Accepted7665ec01 private-preview behavior is historical,
not activation-compatible under this new mandatory gate. Local Node B first,
R2 ready; no current R2/Worker/WAF/paid activation, live transcoding or duplicate
single/batch pipeline. Keep artist/admin authority and other owners independent.
This documentation stage installs nothing; staged implementation follows parent/
Astra review. Exact runtime/migration/settings/grant effects remain separately
coordinated. Preserve accepted artifacts and use isolated forward commits.

## Repository role

shadify_api will become the server-authoritative backend for Shadify. The repository is still architecture-first. Do not scaffold the API, database, media worker, payments, or production deployment unless that implementation phase is explicitly requested.

Before any media/storage work, read:

- docs/SHADIFY_MUSIC_INGEST_PLAYBACK.md (canonical current contract)
- docs/R2_SIGNED_MEDIA_DELIVERY.md (historical R2 preparation)
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
or infer entitlement. Playback HTTP/repository/guest ingress remain inactive. Artist mission schema
adds readiness/publication metadata but no derived producer; do not fabricate
ready output or expose playback without approved repository/ingress wiring.
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

Current source scope includes canonical shell-admitted identity/provenance,
artist memberships, app-owned primary/delegated admin, artist/profile/content lifecycle,
private original uploads and signed GET provider preparation. Artist authority
and status are defined in docs/ARTIST_PROFILES_AND_ACCESS.md. Shadify owns the new primary/delegate rows and admin audit; historical
full-admin grant rows remain inert. No live grant/schema activation occurred. Playback HTTP,
processing, purchases/entitlements/payments and Worker deployment remain pending.

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
it is the single contract and implementation-status source. Stage 1 was accepted
by the parent's independent Astra review; isolated data/API work is authorized.
Reuse canonical Authentication and per-page memberships; never infer is_artist,
admin or durable media access from browser fields or uploader IDs. Ownership,
publication and verification stay independent. Isolated database tests/migration
scripts are allowed; live migrations, grants, cloud effects and shared topology
changes require separately coordinated authority. Frontend work is limited to
documentation references; the other owner retains UI/session integration.


## Current continuation boundary (2026-10-04)

The operator approved app-owned administration after staged Astra review
01a105a1-7d4c-754c-9eda-1d1c71a52948, superseding the intervening central
app_authority design. Authentication supplies immutable admitted identity/session/
assurance only. Shadify owns one configured primary, explicit delegated existing-
operation capabilities and append-only admin audit. Follow
docs/ARTIST_PROFILES_AND_ACCESS.md for exact permissions, locking and rollout.
Never import historical full-admin rows or guess authority from email/first user.
Recheck identity after waits/provider work and audit writes before commit.
Keep Auth, Node Agent, shared libraries and UI with their owners. Use an isolated
checkout for this work; no live source reload, push, build feed, installation,
credential, grant or live migration is authorized. Each app has independent DB
and access on the existing cluster; WSL and node data stay separate. Exact roles,
protected bootstrap/recovery and deployment remain owner decisions.

## Local-first storage approval (2026-10-04)

The operator now explicitly authorizes source implementation of LocalAdapter
behind a small StorageService with the existing R2 adapter retained. This
supersedes earlier R2-first, stateless durable-media and direct-upload-only
rules for this approved phase. Durable local bytes stay outside Git/build/DB;
logical keys, per-object provider, ownership and private/paid/publication rules
stay authoritative. Read the current local contract in
docs/MEDIA_STORAGE_ARCHITECTURE.md and docs/MEDIA_DELIVERY_CACHE_SECURITY.md.
The byte listener is separate and narrowly routed by Node ownership; never
disable GuardedASGI or pass large uploads through Authentication. No live root,
key, credential, listener, migration, installation, cloud call or push is
authorized here. Storage source work precedes any pending admin nomination
implementation; this remains the sole isolated API writer.

## Generic public ingress and local identity (2026-10-23)

Prepared public aliases live inside the existing protected API at /api/public.
Authentication's explicitly configured generic public proxy forwards service-peer
requests without inventing a user assertion. Shadify owns public visibility and
approved media checks; private routes still require admitted user identity.
Coordinate API/catalog/protected selection and frontend before runtime promotion.
Source publication never applies migrations or starts services.

Global Authentication account_id maps to shadify_users.auth_account_id (NOT NULL
UNIQUE, migration002); shadify_users.id is a local UUID. Artist ownership uses
artist_memberships with one active owner per artist (004); manager grants key
(artist_id,user_id) with explicit capabilities (015). Primary/delegate permissions
remain Shadify-owned (006), never inferred from browser headers or Auth app roles.
Canonical domain_app/proxy_app and admission checks remain required. No second
login system or email-based identity mapping is introduced.

Selected overlay recovery provenance is recorded in
 docs/SELECTED_OVERLAY_RECONCILIATION.json. Keep current main's systemd credential
ACL validation and MP4 behavior when reconciling the older selected overlay.
ProjectedReceiver remains explicitly OFF-only; ON managed UDS retains replay.

## Transport 0.1.13 source checkpoint

See [API foundation transport qualification](docs/API_FOUNDATION.md#transport-0113-source-qualification--2026-10-23)
for the exact official artifact, preserved canonical receiver contract, offline
proof limits, and pre-existing unsupported optional WSL adapter. This source-only
update grants no runtime identity, ACL, key, replay, policy or lifecycle changes.
