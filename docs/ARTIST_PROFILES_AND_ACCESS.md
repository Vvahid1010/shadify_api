# Artist profiles and access

Status: Stage 1 accepted with corrections by independent Astra review
`01a10343-cde5-76e5-a121-fd46c7f91471`, relayed by parent on 2026-10-03.
This is the single source of truth for the authorized artist-page backend mission.
Stage 2 isolated implementation is authorized; advance stages after their acceptance. This document describes proposed
behavior unless the implementation ledger explicitly marks it implemented.

## Evidence and scope

Inspected API main `715a8227d67b9805ba6508ffb3ada2fc58858400`, migrations
001/002, domain/repository/media/auth/runtime code, AGENTS.md, local_bootstrap.md,
API_FOUNDATION.md, PROFILE_DATABASE_DEPLOYMENT.md and the three media contracts.
Inspected frontend main `09a8b9d279c05dde75876f65dcc53514c2af7db6`, its AGENTS.md,
SHADIFY_PRODUCT_UX.md, SHADIFY_FRONTEND_ARCHITECTURE.md,
AUTHENTICATION_INTEGRATION.md, typed models, local providers and catalog fixtures.
Both repositories were fetched safely; local/remote divergence was 0/0. Existing
frontend component/browser-test edits belong to the other owner and are untouched.

Current backend: one required `artists.owner_id`, draft tracks, user identity
projection, private audio/artwork original uploads and upload completion. Media
metadata/completion also require the original uploader. There is no persisted
artist profile, membership, admin grant, release, video, event, Moment, layout,
follower, paid entitlement or publication implementation. Frontend Artist,
Release, Moment, profile/layout and pricing data are local fixtures. They are
product vocabulary, not evidence of server permissions or persisted features.

This mission owns shadify_api and documentation references in shadify_web only.
Existing Authentication stays independent. Node Agent retains priority and owns
managed admission, installation, protected runtime configuration and routing.
No UI, shared topology, Auth enrollment, live database migration, admin grant,
cloud resource, secret, Docker, payment execution or CI workflow changes follow
from this contract. Source/package publication needs its own scoped authorization;
do not include unrelated workspace changes in a push.

## Identity and authority decisions

One canonical Authentication identity can be a listener and own many artist pages.
There is no global `is_artist`, second account, team, manager role or generic RBAC.
An artist page has a stable UUID and case-normalized globally unique slug; it
exists independently of its owner and starts unassigned. UUID never changes.
Slug stays stable through ownership changes; initial scope has no slug rename.

`artist_memberships` is the sole page-management authority. Its only role is
`owner`. A partial unique database index on artist_id where revoked_at IS NULL
allows zero or one active owner. Membership references a Shadify user projected
from a canonical Auth account; creator/uploader fields remain audit only.
Account deletion/revocation must never cascade pages, works or media. Preserve
the local identity tombstone and end access; removing a membership preserves
the page and its content. No browser-provided role grants access.

App-local admin is a separate trusted server grant referencing a known projected
identity. No default grant, environment boolean, self-service endpoint, central
Auth role inference or per-page admin membership. Grant creation/revocation is
an operator-controlled administrative operation outside this mission's runtime
activation. Admin acts as themselves; every management result identifies actual
actor, target page and whether admin authority was used. No impersonation.

Assignment accepts an existing verified canonical identity ID, never an email,
display name or arbitrary new account. The current Auth API exports self-account
operations, not a cross-account directory. Proposed bounded initial rule: the
target must already exist in the Shadify user projection from a successful
canonical admitted request, must not be locally revoked/tombstoned, and must have
recorded canonical admission provenance, written only by the trusted server
admission adapter. An existing shadify_users row alone is insufficient: the old
projection recorded no provenance. Assignment never creates a user or proof.
Unknown or unverified targets fail closed. Historical admission does not prove
the account still exists centrally: automated central deletion/revocation sync
and a current trusted resolver remain owner-coordinated integration gaps. Do
not query/import Auth's private database or call its internal service directly.
Astra accepted this previously-observed-identity rule. Every management request
still requires current valid canonical admission; no immediate central revocation
claim is possible without trusted synchronization. A disabled local identity loses
management regardless of membership; locally revoked users cannot self-reactivate
by another projection. Membership removal and central account deletion are
distinct operations, neither deletes page content.

## Permissions and lifecycle

| Actor | Public published page | Private page/content | Edit ordinary content | Publish ordinary content | Owner changes | Suspend / verify |
| --- | --- | --- | --- | --- | --- | --- |
| Guest/listener without membership | Filtered metadata | No | No | No | No | No |
| Active owner of this page | Filtered metadata | Yes | Yes | If ready and page not suspended | No | No |
| Owner of another page | Filtered metadata | No | No | No | No | No |
| Active app admin | Filtered metadata | Yes, including unassigned | Yes | If ready; explicit unsuspend required | Yes | Yes |

Ownership, page publication (`draft`, `published`, `suspended`) and official
verification are independent. Admin can create, prepare and publish an unassigned
page; a published unassigned DTO computes `curated_by_shadify: true`. This field
is not persisted or accepted in writes. Verification means an explicit admin
decision, never ownership, payment, publication or a user-editable badge.

Owner may edit ordinary metadata/drafts while suspended, but cannot publish
content, clear suspension or self-verify. Admin suspension retains prior content
states privately; resumption requires an explicit admin publication operation.
Generic PATCH does not accept owner, memberships, grants, verification,
publication state or actor identifiers. Reject unknown fields. Ownership changes
leave suspension and verification untouched.

Admin assignment (only if unassigned), transfer (expected current owner required)
and revocation (expected current owner required) are separate atomic operations.
Lock the stable artist row in a transaction; verify actor/admin, expected owner
and target identity; revoke old membership, insert new membership if any, and
append audit before commit. Unique-index enforcement also protects direct races.
A stale expected owner returns 409. Audit records immutable event ID, artist ID,
actual actor, action, old/new owner account IDs and UTC timestamp. No credentials,
tokens or contact/security fields in audit. Failure rolls back the whole change.
Preserve IDs, slug, works, follower references and object keys. No payout, purchase
history, copyright or entitlement changes are implied by a transfer.

All management reads/writes take the same artist row lock and recheck current
identity and authority within their transaction. They also lock actor/target
projection rows in sorted account-ID order and the actor's admin-grant row until
commit. A trusted tombstone or grant revocation updates those rows and therefore
serializes with the operation; an artist lock alone is insufficient. Grant/identity
revocation must not reverse this lock order by acquiring artist rows afterward.
Concurrent revocation tests cover actor, target and admin-grant validity. Authorization and commit must not be
separate unchecked database operations. After transfer commits, old owner cannot
read new private state or perform another write. A read authorized before commit
may have already returned bytes; no retroactive revocation claim. Private read
queries join current membership/admin and target scope in one database snapshot;
responses are no-store. Never cache owner capability as authority.

## Data and compatibility contract

Use additive, explicit SQL migrations, manually applied only to an isolated test
database in this mission. 003 adds identity provenance without fabricating it;
004 cuts over ownership only after actual trusted provenance exists for every
legacy owner. Missing/ambiguous legacy proof aborts 004 atomically. Existing rows
alone do not unblock it. Future operational preparation must be explicitly
coordinated; no source task may manufacture proof to make migration succeed. An operational schema/package activation is separately
coordinated; startup never runs migrations. Backfill each legacy owner into one
active membership only when it maps unambiguously to a previously verified local
canonical user. Abort migration on unresolved legacy owners and report IDs/counts
without guessing or creating accounts. Preserve all existing IDs and object keys.
After cutover, no code path reads artists.owner_id as authority. Remove its
NOT NULL requirement and retire the legacy column in the same coordinated schema
change; uploader owner_id remains explicitly audit data. Old package readiness
queries use artists.owner_id, so deployment must pair matching schema and package;
this source work does not promise mixed-version compatibility or live migration.

| Concept | Smallest persisted contract |
| --- | --- |
| User projection | Existing UUID / unique Auth account ID, canonical-admission provenance and local active/tombstone state; public name, slug, bio and genres only. No Auth credentials/contact/security fields. |
| Artist | Existing UUID, unique slug, name, bio, genres, safe social links, nullable avatar/header asset references, publication state, verification state. |
| Membership | UUID, artist FK, projected user FK, role owner, assigned/revoked UTC timestamps; one active owner index. |
| Admin grant | Projected user FK, active/revoked state; trusted operator provenance. No HTTP grant endpoint. |
| Ownership audit | Immutable actor/page/old owner/new owner/action/time; no delete cascade. |
| Track | Extend existing artist-scoped row with ordinary metadata, free/paid access class, draft/published state and explicit original/delivery/preview asset references. |
| Release | Stable artist-scoped ID, title, description, Single/EP/Album, cover, publication; ordered track join with unique order/track and same-artist foreign keys. |
| Video | Stable artist-scoped ID, music_video/live_performance kind, title, optional same-artist music link, asset reference and publication. |
| Moment | Existing product concept, artist-scoped official news with actual author audit and optional same-artist music reference. No parallel blog/news entity. Ordinary user mentions never confer official provenance. |
| Event | Artist-scoped ID, title, UTC instant plus valid IANA timezone, city, venue, optional HTTPS ticket URL and cancellation flag. No booking/settlement system. |
| Layout | Artist-scoped featured work/video, pinned official Moment, allowlisted section order/visibility. References must belong to the same artist. |

Work/media belong to stable artist IDs; membership is never their owner foreign
key. Use composite target_id/artist_id constraints where possible and authoritative
scope checks for every reference. Disallow ownership/page reassignment in generic
content edits. Public user-profile administration exposes only these public
Shadify fields and cannot inspect another account's security or contact data.
Self-profile editing is separate from artist administration and never grants access.

Reuse frontend section IDs `featured`, `songs`, `video`, `moments`, `bio`, `tour`.
`merch` is reserved/unsupported, not a commerce feature. Return section availability
and empty state separately from requested visibility. Featured works are track or
release IDs with an explicit kind. No arbitrary executable layout, HTML, embed
URLs or unbounded section IDs. Reject duplicate section IDs/order positions and
cross-artist feature/pin references. Text is plain text; URLs must be validated
HTTPS links. Request schemas reject extras and apply these initial limits:
slug 3–64 ASCII lowercase letters/digits/hyphens, starting/ending alphanumeric;
name/title 1–200 trimmed characters (artist name maximum 120); bio/description/
Moment text maximum 5,000; at most 20 distinct genres of 1–64 characters; at most
10 social links of maximum 2,048 characters each. Reject text control characters
except newline in long text. Pagination defaults to 20, maximum 100, using a
stable UUID cursor. Releases contain at most 100 distinct ordered tracks. Layout
contains at most the six supported sections, with distinct positions. Events
require an offset-aware instant and a ZoneInfo-valid IANA timezone; city/venue
maximum 200 characters and ticket URL maximum 2,048. Do not fetch external URLs.

Artist avatar/header and video attachment can be represented without pretending
the current track-scoped audio/artwork upload API supports them. Initially return
their upload/processing availability as false until a reviewed media-intent
extension exists. No new video pipeline, background service or synthetic ready
asset is authorized. Optional profile media can remain absent on a published page.
Track/video publication requires an actual ready derived delivery asset. Originals
or successful upload completion do not prove processing/readiness. Release
publication requires every included track to satisfy publication readiness; page
publication filters private child content. Paid metadata can exist without a
payment engine; paid full playback remains denied without existing entitlement
proof. Management permission never implies playback entitlement. Future playback
projections must also carry authoritative artist page publication status; new URL
issuance requires the page to be published and not suspended as well as eligible
track/asset states. Suspension hides children without rewriting their publication
states. Already issued bearer URLs remain valid until their existing TTL. The
prepared PlaybackTarget adapter must fail closed when page status is absent;
this requirement does not activate a playback HTTP route or guest ingress.

Official platform playlists and home rails require app admin authority. Current
frontend fixtures do not implement these backend resources. Do not add an extra
curation system in this mission; advertise their unavailable status explicitly.
Follower counts/references also have no persisted backend implementation yet;
never convert fixture counts into authoritative data or delete future references
on ownership changes.

## API contracts (proposed; unversioned)

Existing `/api/me`, artist tracks and media routes stay; integrate the shared
page authorizer rather than creating parallel ownership endpoints. UUID parse
errors/invalid bodies return 422. Missing/unauthorized private targets return 404
uniformly (existing track denial changes from 403 to 404); unauthenticated requests follow canonical admission. Admin-only actor
denial returns 403 without enumerating private pages/users. Lifecycle races or
ineligible publication return 409; integration/provider unavailability returns
redacted 503. All private/mutation responses use no-store. Public metadata is
no-store in this initial scope; no new caching framework.

| Method and path | Behavior |
| --- | --- |
| GET /api/artists/{artist_id}/public | Filtered published metadata; draft/suspended absent, no membership IDs, audit, original keys, private assets or signed URL. |
| GET /api/artists/by-slug/{slug}/public | Same filtered projection via normalized unique slug. |
| GET /api/me/artists | All currently manageable pages; one person may own several; bounded pagination; actual actor and computed per-page capabilities. Admin scope may explicitly include unassigned pages. |
| POST /api/admin/artists | Admin creates unassigned draft page; supplied owner/admin flags rejected. |
| GET/PATCH /api/admin/users/{user_id}/profile | Admin reads/edits existing user's public Shadify profile fields only; no account lookup directory, contact/security access or identity creation. |
| GET /api/artists/{artist_id} | Current owner's/admin's private page and capabilities. |
| PATCH /api/artists/{artist_id} | Ordinary profile metadata only; reference scope validation and locked reauthorization. |
| PUT /api/artists/{artist_id}/layout | Same-artist feature/pin references and allowlisted sections only. |
| POST /api/admin/artists/{artist_id}/owner | Assign existing verified target account_id to unassigned page. |
| PUT /api/admin/artists/{artist_id}/owner | Transfer to verified target; expected_owner_account_id prevents stale overwrite. |
| DELETE /api/admin/artists/{artist_id}/owner | Revoke expected owner using a validated request body; no content deletion. |
| PUT /api/artists/{artist_id}/publication | Owner/admin chooses draft/published, subject readiness and suspension restrictions. |
| PUT /api/admin/artists/{artist_id}/suspension | Admin suspends or explicitly resumes to draft/published; no owner override. |
| PUT /api/admin/artists/{artist_id}/verification | Admin changes independent verification state. |
| GET /api/admin/artists/{artist_id}/ownership-audit | Admin-only bounded audit history; actual actor retained. |
| GET/POST /api/artists/{artist_id}/{resource} | Private list/create for tracks, releases, videos, moments, events; no public list of draft content. Existing track paths reused. |
| GET/PATCH/DELETE /api/artists/{artist_id}/{resource}/{id} | Scoped private read/edit/delete; reference/dependency conflicts reject deletion, no artist cascade. |
| PUT /api/artists/{artist_id}/{resource}/{id}/publication | Shared technical/publication checks for publishable works/Moments; events use cancellation instead. |
| POST /api/media/uploads; POST /api/media/uploads/{id}/complete; GET /api/media/{id} | Existing interfaces, now current page authority rather than uploader authority; no key substitution or unrelated media read. |

Private DTO envelope identifies `actor_account_id`, `artist_id`, `acting_as_admin`
and computed capabilities such as edit_profile, manage_content, publish_content,
change_owner, suspend and verify. These communicate server decisions for one
shared Studio; request-supplied capability fields are rejected. Public DTOs omit
actor/security context and expose only published child records and safe references.
Private metadata GET does not become a signed private original GET endpoint.
Any later private signed GET must use this same authorizer; playback remains the
separate existing delivery authorization boundary.

Public source-ASGI metadata routes do not imply operational guest access. Current
managed native admission is Authentication-peer/account scoped. Guest enrollment,
receiver catalog approval and Auth/Node Agent routing for new routes are parent/
owner integration requirements. Do not weaken native admission, fabricate an
authenticated guest, expose loopback directly or change shared topology here.
App-owned catalog changes can be prepared after review; live enrollment is separate.

## Transfer-safe uploads and private media

Preserve R2_SIGNED_MEDIA_DELIVERY.md: private R2, short-lived PUT/GET, stable keys,
no Worker, public bucket or mandatory custom domain. Credentials remain protected
server runtime inputs; no new secret field, credential read or cloud effect.

Issuance checks current page authority and same-artist track. Store uploader as
audit, not durable access authority. Recheck current permission before remote
completion inspection and again under the artist lock when committing the state
transition/attachment. A transfer during HEAD/inspection cannot authorize the
old owner to finish afterward. New owner or admin can complete the original
pending upload if still valid, without copying objects or changing object keys.
Idempotent completion also reauthorizes current access. A failure-state write
after inspection must reauthorize too. Attachments validate target page, asset
page, kind and allowed technical state within the same locked transaction.

An already issued bearer PUT/GET can remain usable until its existing short TTL;
transfer cannot retroactively revoke it at R2. A stale uploader may send bytes,
but cannot complete, attach, read private metadata or obtain a new authorization.
Do not claim single-use URLs, instant cloud revocation or real R2 verification.
Asset deletion/detachment must not delete a referenced original or schedule an
unimplemented cloud cleanup service. No object deletion is exercised in tests.

## Future shared Studio requirements (no UI work here)

Use one shared Authentication session and selectable manageable-page context.
Include unassigned curated pages for admins and all owned pages for a listener.
Fetch capabilities for the selected stable page; do not infer access from a
button, fixture userId, uploader or a global artist/admin boolean. Display actual
admin actor context, independent ownership/publication/verification state and
availability/empty states. On transfer/revocation refresh selection and handle
server denial immediately. Ordinary mentions must not display official news.
Forms must not imply working video uploads, processing, purchases, analytics,
followers or official rails where the backend reports them unavailable.

## Staged acceptance and implementation ledger

1. **Contract:** this single document, API instructions link and brief frontend
   reference; current gaps, permissions, schema/lifecycle/API and test matrix are
   reviewable. Parent obtains independent Astra acceptance before Stage 2.
2. **Data and authorization:** reviewed additive migration and single shared
   authorizer; verified-projection/admin provenance; isolated PostgreSQL unique
   owner/concurrency/backfill/rollback proof. No operational migration/grants.
3. **Core APIs:** page lifecycle/profile/layout and scoped content/media operations
   follow the accepted model; actual readiness gates, public filtering, transfer
   rechecks and audit work. Every unavailable feature remains explicit.
4. **Tests/handoff:** real isolated database concurrency/IDOR/lifecycle tests and
   focused source/API tests; baseline managed ingress/replay/native package tests
   still pass. Report schema/package/enrollment/config prerequisites separately
   from source proof. R2/VM/central identity integration awaits owner setup.

Prepared independent acceptance inputs live in
`tests/fixtures/artist_access_acceptance.json`; they are expected outcomes, not
passing implementation tests. Cases must execute against real migrated isolated
PostgreSQL for transactional assertions and the API for scope/DTO checks:

- Admin creates unassigned draft, adds metadata/draft content and publishes page;
  public page is curated, retains unpublished children privately.
- Assignment accepts a known canonically verified identity only, creates no user;
  multiple pages per user; unknown/tombstoned/unverified identities rejected.
- Concurrent assignment/transfer never yields two active owners; stale transfer
  409; audit+membership rollback together on failure.
- Old owner loses all management after commit; new owner/admin retains stable
  works, keys, slug and verification/suspension; no financial side effects.
- Cross-page IDs fail for private reads, content create/edit/delete/publish, media
  issue/complete/metadata, release track joins and layout/asset attachments.
- Upload completion paused across transfer cannot commit for old actor; new owner
  can complete; repeated completion and failure writes reauthorize.
- Publication, ownership and official verification remain independent. Owner
  cannot grant admin, verify self, unsuspend or publish technically unready media.
- Draft/suspended/private/original assets never leak through public page/slug DTO;
  ordinary mentions are not official Moments; paid playback denies absent proof.
- Revocation or user tombstone preserves page/content/audit; no destructive FK
  cascade. Reserved commerce/video/profile upload features stay unavailable.

Actual results, 2026-10-03: Stage 1 documentation and 22 pending acceptance inputs
prepared; JSON parses and scoped diff whitespace checks pass. Existing source
suite rerun with `.venv/bin/python -m unittest discover -s tests -v`: **52 passed**,
including managed admission/replay and stubbed S3 signing/inspection. No real cloud
or operational database was used. These are baseline regressions, not proof of
the new artist cases. Stage 2 isolated data/authority acceptance is met; Stage 3 API integration and Stage 4 complete handoff remain pending. Remaining integration decisions: schema/package cutover handling and public
guest/catalog owner enrollment. The bounded verified-target rule was accepted. Real cloud, VM, admin bootstrap and central
deletion-sync checks remain pending regardless of local test results.

### Stage 2 evidence (2026-10-03)

Migrations 003/004 and `access.py` implement trusted admission provenance,
non-reactivating local tombstones, single active owner, separate trusted admin
grants, append-only ownership audit and artist/identity/grant transaction locks.
`test_artist_postgres.py`: **8 passed** against a fresh native PostgreSQL 18
instance with private temporary Unix socket, no TCP listener, disposed afterward.
Proof includes simultaneous assignment + unique index, stale owner/audit rollback,
old owner denial, actor/target/grant revocation waiting until commit, preservation
on revocation/tombstone, and legacy row-only cutover abort followed by synthetic
trusted-observation fixture backfill. No real identity, admin grant or operational
database was used. Existing routes are integrated in Stage 3, not this milestone.
