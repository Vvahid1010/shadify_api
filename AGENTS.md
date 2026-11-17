# Shadify API guide

روی main کار کنید و از شاخه‌های اضافی پرهیز کنید
از worktree و نسخه‌های موازی غیرضروری پرهیز کنید

Optional background: [shared DEV simplicity policy](../node_agent/AGENTS.md#dev-simplicity-policy---operator-decision-2026-10-10).
This guide is sufficient for ordinary work in a standalone clone. Cross-repository
links are optional background, not prerequisites; consult the owning contract only
when changing that integration. Missing sibling checkouts do not block local work.
Routine isolated tests, local UI checks, task-needed development dependencies and
passive health checks of an already running authorized service need no repeat approval.
Use pinned dependencies and disposable fixtures; do not alter a service environment,
start a retired runtime, access real credentials, trade, migrate persistent data or
change production/privileged state outside the requested or already authorized scope.
Complete the requested flow with existing patterns and proportional checks; preserve
others' work, security, data and runtime authorization. Source/tests/publication
are distinct from installed behavior and user acceptance. Historical checkpoints
are evidence, not new work orders. Read deeper context only for the task at hand.

## Outcome and application authority

The agreed DEV WSL outcome is a real approved upload, ingest/playback and sample
artist profile through central Authentication. Fixtures/tests alone do not prove
that outcome. Node Agent is the execution platform; do not make unrelated Topology
redesign or generic qualification frameworks prerequisites.

This is an implemented API, beyond the earlier architecture-only phase. Source
includes admitted identity, memberships, app-owned primary/delegated administration,
artist/profile/content lifecycle and storage preparation. See
`docs/API_FOUNDATION.md`, `docs/ARTIST_PROFILES_AND_ACCESS.md` and
`local_bootstrap.md` for implementation status and missing real integration proof.
A historical source checkpoint is not proof of current runtime activation.

Reuse central Authentication, never a second login or email-based mapping.
`shadify_users.auth_account_id` is the NOT NULL UNIQUE global identity (migration
002); local user IDs are UUIDs. Artist membership has one active owner (004);
manager grants use `(artist_id,user_id)` and explicit capabilities (015).
Primary/delegate permissions remain app-owned (006), not Auth app roles/browser
headers. Preserve domain_app/proxy_app and canonical admission. One configured
primary, delegated existing-operation capabilities and append-only audit replace
historical full-admin grants; never infer authority from email/first user or import
old full-admin rows. Recheck identity after waits/provider work and audit before
commit. Ownership, publication and verification are independent.

Shadify owns content/publish state, support/purchase relations and entitlements;
future Chapary integration executes payments only. Browser/uploader IDs never
prove artist ownership or media rights. Auth, Node Agent, shared libraries and UI
retain their owners. Each app has independent DB/access; WSL and node data differ.

## Current media contract

Read `docs/SHADIFY_MUSIC_INGEST_PLAYBACK.md` before upload/media/matching/processing/
playback; its eleven-section contract supersedes historical readiness/storage
claims. Every upload is untrusted, including artist/admin and single/batch on
PC/Android: quarantine → mandatory open-source server malware scan → validation →
extraction → metadata-only MusicBrainz matching/review → ingest-once processing →
approval. No preview, playback, usable file delivery or publication before approval.
Uploaded is not ready; the old private-preview behavior is historical. Keep one
single/batch processing pipeline. The contract stage grants no installation/live
transcoding/cloud/settings effects. Ordinary scoped source work and isolated checks
need no mandatory parent/model review; review sensitive boundaries proportionally.

Local Node B comes first, with R2 ready. The later local-first StorageService /
LocalAdapter approval supersedes old R2-only, stateless-node and direct-upload-only
requirements for this phase. Keep existing R2 adapter and per-object provider,
logical keys and ownership. Durable local media stays outside Git/build/DB. The
separate narrow byte listener is Node-owned: never disable GuardedASGI or proxy
large upload bodies through Authentication. No paid/R2/Worker/WAF activation is
implied. Read `docs/MEDIA_STORAGE_ARCHITECTURE.md` and
`docs/MEDIA_DELIVERY_CACHE_SECURITY.md` for the active storage/delivery boundary;
`docs/R2_SIGNED_MEDIA_DELIVERY.md` retains R2 preparation context.

Originals are private and immutable-by-key; replacements make new objects. Derived
assets/previews/artwork are separate. DB stores metadata/keys, never large binaries;
GitHub is not storage. Server-generated keys, MIME/container/size validation and
short-lived upload/access authorization remain mandatory; browser ownership/keys
are untrusted. Keep provider details behind the existing abstraction; no speculative
provider/tier or global cache/rate-limit framework.

Approved published free tracks allow checked guest playback; paid full assets need
Auth and proven purchase/entitlement, with separate preview assets. Never fabricate
readiness or implement payments from a playback request. Player gets opaque URL +
expiry; Track/Playlist persist stable IDs, not signed URLs. Presigned URLs can repeat
and remain reusable until protected-config TTL; no IP lock. Keep originals/private
assets nonpublic, secrets/URLs out of logs, and cache distinct from authorization.
R2 preparation uses private Standard storage and no public bucket/custom-domain
requirement; application WAF does not imply direct R2 protection. Preserve future
issuance abuse-control and provider-adapter boundaries without activating them.

## Source, runtime and transport

Work in `/srv/Coding_space/shadify_api`, native WSL Python 3.14 and source `.venv`,
no Docker/Compose. Preserve Python3.14 Linux x86_64 source/offline-wheel/hash-manifest
packaging. Node Agent owns `shadify-api`/identity `shadify_api` on selected Node B,
protected config, transport/checkpoint/replay and lifecycle. `shadify.org` is the VM
hostname; `dev.shadify.org` is separate WSL. `media.shadify.org` remains reserved
for future Worker/CDN. User supplies Cloudflare/storage keys through the protected
runtime route, never chat. Do not independently modify Node/Auth/shared topology.
Isolated data/API tests and migration scripts do not authorize live migrations,
grants, credentials, listeners, deployment or runtime reload. Preserve task-specific
source/publication restrictions until superseded by the user's current scope.

Canonical shell admission is the identity boundary; no separate HMAC crypto fork.
Prepared `/api/public` aliases accept configured service-peer public proxy calls,
not invented user assertions. Shadify checks public visibility/approved media;
private routes still need admitted identity. Coordinate catalog, protected selection
and frontend before activation. Use `docs/IDENTITY_PROJECTION.md` for optional
site-bound Auth facts; projection grants no app authority.
Keep `docs/SELECTED_OVERLAY_RECONCILIATION.json` provenance and current main's
systemd credential ACL/MP4 behavior when reconciling older overlays.
ProjectedReceiver is OFF-only; ON managed UDS keeps replay. Official transport
0.1.13 qualification/limits are in
`docs/API_FOUNDATION.md#transport-0113-source-qualification--2026-10-23`.

Update storage/security docs when those contracts change. Source publication never
applies migrations or starts services; report actual integration proof separately.
