# Artist profiles and access

Status: Stage 1 accepted with corrections by independent Astra review
`01a10343-cde5-76e5-a121-fd46c7f91471`, relayed by parent on 2026-10-03.
This is the single source of truth for the authorized artist-page backend mission.
Stages 2–4 have met local source/package acceptance at reviewed implementation
commit `053d93bc9b145871fae4895d6bcbadf4dd565bef`. Independent Astra limited PASS
applies to that exact implementation, not operational activation or the later
central-admin consumer. The operator subsequently approved app-owned administration after staged Astra
review `01a105a1-7d4c-754c-9eda-1d1c71a52948`. This supersedes the intervening
central-admin consumer architecture. Authentication provides identity, session
and assurance; Shadify owns primary/delegated permissions and admin audit.
This continuation is source/local commits/isolated tests only; no source/build
publication, live migration, installation or real authority selection is authorized.
The implementation ledger distinguishes tested source behavior from operational integration.

## Evidence and scope

Inspected API main `715a8227d67b9805ba6508ffb3ada2fc58858400`, migrations
001/002, domain/repository/media/auth/runtime code, AGENTS.md, local_bootstrap.md,
API_FOUNDATION.md, PROFILE_DATABASE_DEPLOYMENT.md and the three media contracts.
Inspected frontend main `09a8b9d279c05dde75876f65dcc53514c2af7db6`, its AGENTS.md,
SHADIFY_PRODUCT_UX.md, SHADIFY_FRONTEND_ARCHITECTURE.md,
AUTHENTICATION_INTEGRATION.md, typed models, local providers and catalog fixtures.
Both repositories were fetched safely; local/remote divergence was 0/0. Existing
frontend component/browser-test edits belong to the other owner and are untouched.

Baseline at Stage 1 inspection: one required `artists.owner_id`, draft tracks, user identity
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
Slug stays stable through ownership changes; initial scope has no slug rename. Legacy pages receive `artist-<UUID>` on
cutover; empty legacy names remain draft until an authorized editor supplies one.

`artist_memberships` is the sole page-management authority. Its only role is
`owner`. A partial unique database index on artist_id where revoked_at IS NULL
allows zero or one active owner. Membership references a Shadify user projected
from a canonical Auth account; creator/uploader fields remain audit only.
Account deletion/revocation must never cascade pages, works or media. Preserve
the local identity tombstone and end access; removing a membership preserves
the page and its content. No browser-provided role grants access.

Admin authority belongs to this Shadify database. Canonical Authentication
admission still supplies immutable account/session/assurance, exact Shadify
audience and issued/expiry times. The Identity has no admin field. Central
`app_authority` members are ignored for app permissions; forged headers, ordinary
user fields, auth_level, emails and display names cannot grant authority.
Duplicate assertion JSON keys still fail admission. Recheck identity after SQL
locks/provider work and after audit writes before commit. The historical central
admin-only 60-second lifetime cap no longer applies: identity follows existing
identity expiry rules; permissions are read freshly from Shadify under SQL locks.

Additive **006_app_admin** creates one fixed app-state row, initially without a
primary, explicit delegate rows and append-only app admin audit. A configured app
has exactly one primary; an unconfigured app can have none. No legacy
`shadify_admin_grants` row or central claim is imported or used as fallback.
Those historical full-admin rows stay inactive and untouched. Migration does not
select an operator/account and cannot promote the first user. Current actor and
grant/transfer targets must be active previously canonically observed identities.

The primary can delegate any subset of the eight actual-operation capabilities
below, replace/revoke a delegate's subset, and transfer the primary to an explicit
valid target. Delegates cannot change grants, redelegate, promote themselves or
replace the primary, even if given every operation capability. Grant replacement
uses explicit arrays (empty means revoke); unknown/duplicate permissions fail.
Primary transfer requires expected current primary and a mandatory `retain` or
`revoke` delegate decision. The incoming primary's delegate row is removed; the
former primary receives no automatic delegate grant. State/grant changes and
audit commit together. There is no delete-primary operation; a database trigger
rejects clearing/deleting configured primary state. Local account security
deactivation is still allowed and denies access without deleting primary state.

Authority mutation locks app state FOR UPDATE, then actor/target identity rows in
account order. Artist operations lock artist, app state FOR SHARE, sorted
identities, then referenced resources. Grant/primary changes serialize with
transactions already holding SHARE: a change waits for that operation to commit;
operations obtaining the state after revocation deny. Provider work holds no SQL
transaction; its later insert/transition reauthorizes freshly, so revocation while
the provider runs prevents success and failure writes. This is local transaction
ordering, not instant central account/session revocation or cloud URL cancellation.

Only a protected local, unwired `bootstrap_primary` preparation seam exists. It
requires explicit fresh target identity, recorded provenance and operator
reference, fails if already configured, and audits atomically. Tests use synthetic
accounts only. No HTTP bootstrap, recovery endpoint, real grant, credential,
operator-authority selection or automatic recovery was added. Recovery for a
deactivated/lost primary requires a separately selected protected owner procedure.

New primary-only routes: GET `/api/admin/authority`, GET
`/api/admin/authority/audit`, PUT/DELETE `/api/admin/delegates/{account_id}` and PUT
`/api/admin/primary`. They are app-owned catalog declarations, not live endpoint
enrollment. Delegates cannot read the authority roster/audit. Page capability DTOs
combine independent ownership with the actual permission subset; acting_as_admin
identifies the authority used for the operation. Admin operations record actor,
action and page/target context in append-only app audit. Grant events preserve
before/after subsets; transfer records old/new primary and delegate policy/history.
Existing ownership audit remains independent and transactional. Ordinary content
author/uploader identities remain audit fields, never permission sources.

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
| Primary admin (delegates only within their subset) | Filtered metadata | Yes, including unassigned | Yes | If ready; explicit unsuspend required | Yes | Yes |

| App capability | Existing operations covered |
| --- | --- |
| create_artist | Create an unassigned artist draft |
| edit_profile | Artist profile and layout edits |
| manage_content | Track/release/video/Moment/event metadata create/edit/delete and existing private original upload issue/complete |
| publish_content | Page and track/release/video/Moment publish/unpublish, with existing readiness/suspension rules |
| change_owner | Artist assignment/transfer/revocation and ownership audit read |
| suspend | Explicit page suspension/resumption; resuming as published additionally requires publish_content |
| verify | Explicit page verification/unverification |
| edit_user_profile | Existing public-user profile read/edit; no account/security fields |

Any page capability permits page metadata/capability inspection, including
unassigned pages. Private content/media metadata reads require manage_content or
publish_content (or independent page ownership). Existing upload mutation requires
manage_content. create_artist/edit_user_profile alone do not enumerate/manage other
pages. Event edits retain the existing calendar-metadata visibility contract;
there is no separate event publication permission/endpoint. No generic role graph,
financial capability, processing permission or new media pipeline is introduced.

Ownership, page publication (`draft`, `published`, `suspended`) and official
verification are independent. Admin can create, prepare and publish an unassigned
page; a published unassigned DTO computes `curated_by_shadify: true`. This field
is not persisted or accepted in writes. An inactive identity with an unrevoked
membership remains assigned but cannot manage; curated status is not inferred
from identity inactivity. Verification means an explicit admin
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
actual actor, action, old/new owner account IDs and UTC timestamp. Persistent
ownership audit rows cover assign/transfer/revoke only. Unassigned page creation
identifies the actor in its response and now writes a separate app admin creation
audit event; it does not add an ownership-audit event. No credentials,
tokens or contact/security fields in audit. Failure rolls back the whole change.
Preserve IDs, slug, works, follower references and object keys. No payout, purchase
history, copyright or entitlement changes are implied by a transfer.

All management reads/writes take the same artist row lock and recheck current
identity and authority within their transaction. They also lock actor/target
projection rows in sorted account-ID order until commit. Trusted local identity
tombstones therefore serialize with the operation; an artist lock alone is
insufficient. Local identity revocation must not reverse this order by acquiring
artist rows afterward. Request freshness is rechecked after blocking authority
locks, after external media inspection and immediately before transaction commit;
expiry causes rollback, including ownership audit. Authorization and commit are
not separate unchecked database operations. App grant/primary revocation is
ordered by the local state lock; canonical identity/session revocation remains
dependent on admitted expiry and trusted central synchronization. Existing
storage URLs retain their bounded validity and are not invalidated by grants.
After transfer commits, old owner cannot
read new private state or perform another write. A read authorized before commit
may have already returned bytes; no retroactive revocation claim. Private reads use the same locked authorizer and query target scope in that transaction;
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
| Admin authority | Central Auth app-scoped admitted claim; request-local and never persisted as a local grant. Legacy grant table remains inert. |
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
There is no self-profile editing endpoint in this candidate. `/api/me` reads
the identity projection; admin public-profile editing is not a self-service edit
interface and must not be used as one by the frontend.

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
except newline in long text. Private pagination defaults to 20, maximum 100; public page collections cap at
20 per resource to bound aggregate responses. Manageable-page lists return summaries
and capabilities rather than repeating full biographies/layouts. Pagination uses a
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

## API contracts (implemented in source; unversioned)

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
| GET /api/artist-slugs/{slug}/public | Same filtered projection via normalized unique slug. |
| GET /api/me/artists | All currently manageable pages; one person may own several; bounded pagination; top-level actual actor, acting_as_admin and capabilities.create_artist even when pages is empty; computed per-page capabilities. Admin includes unassigned pages. |
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
| PUT /api/artists/{artist_id}/{resource}/{id}/publication | Shared technical/publication checks for tracks/releases/videos/Moments; events are validated calendar metadata following page visibility and use cancellation instead. |
| POST /api/media/uploads; POST /api/media/uploads/{id}/complete; GET /api/media/{id} | Existing interfaces, now current page authority rather than uploader authority; no key substitution or unrelated media read. |

`GET /api/me/artists` additionally returns top-level `acting_as_admin` and
`capabilities: {"create_artist": <boolean>}` from the same freshly read app-owned primary/delegate
authority. These remain present when `pages` is empty, allowing an authorized
admin to discover page creation without an existing page. Listener/owner-only or
revoked admin identities receive false. No authority is inferred from the response; POST /api/admin/artists independently reauthorizes.

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
`tests/fixtures/artist_access_acceptance.json`; they map independent expected outcomes to actual local acceptance tests.
Those tests do not establish real cloud/VM/central identity integration. Cases must execute against real migrated isolated
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

Stage 1 history: documentation and 22 acceptance inputs were prepared; the
then-existing baseline suite passed 52 tests. The inputs now map to local test
coverage. Stages 2–4 local acceptance is complete: final source and fresh offline
packaged suites each passed **72 tests**, with clean dependency checks. The local
candidate receipt records exact implementation commit053d93bc and schema target005.
These results establish tested code/metadata-management behavior, not live
application operation. Real cloud/VM, schema/package cutover, central admin
issuance integration, central account-deletion synchronization and guest enrollment remain
pending. The bounded previously-observed-identity rule was accepted.

### Stage 2 evidence (2026-10-03)

Historical local-grant implementation below is superseded by the app-owned
primary/delegate foundation (and the intervening central consumer); its original evidence remains bound to the reviewed earlier source.

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

### Stage 3 evidence (2026-10-03)

Core page/profile/layout/ownership/audit APIs, scoped track/release/video/official
Moment/event metadata operations and current-membership media issue/complete/read
are implemented. Native catalog has **49 unambiguous routes**, all still admitting
only Authentication. Slug lookup is `/api/artist-slugs/{slug}/public`: the earlier
`/api/artists/by-slug/{slug}/public` overlapped content-item templates and was
rejected by canonical catalog validation. No live catalog/guest enrollment occurred.

005 adds explicit artist-scoped content/reference tables and derived-asset metadata
eligibility. It creates no processing output or entitlement. Original constraints
retain private/draft invariants. Current upload API remains only track-scoped WAV/
FLAC or JPEG/PNG originals; profile/video upload and processing stay unavailable.
Publication checks trusted metadata readiness, not real object reachability.
Events are validated calendar metadata visible when their page is published,
including cancellation notices; there is no independent event draft/publication
endpoint or booking workflow. Owners can edit them under the same page authority.

Canonical admission records previously observed identity provenance before private
business dispatch. Projection/assignment cannot fabricate it. Every request still
passes canonical admission; local tombstones stay inactive. Current administrator
authority comes only from the new app-owned state/delegates; the old local
full-admin grants remain inert. The original Stage 3 result predates this change. Suspensions/verification use dedicated admin-only actions;
only explicit suspension action can resume a held page, including for admins.

Attachment/readiness validation locks referenced assets after artist/identity
locks until commit; admitted authority freshness is rechecked before commit. Every future trusted producer must use that same order
for page/resource writes; no producer is installed here. Public projection uses a
single read-only repeatable-read snapshot. It omits private originals/uploader and
owner identities, hides draft/suspended pages and private/unready children, drops
optional links to private music/withdrawn artwork, and hides invalid featured refs.
A request that acquired its public snapshot before suspension may complete with
that snapshot; a new snapshot after commit cannot expose the suspended page.

`test_artist_api.py`: **11 passed**, using real disposable PostgreSQL and synthetic
admission/object storage. Covers unassigned curated page, official Moment drafts,
multiple pages, admin/non-admin isolation, canonical observation before assignment,
HTTP transfer/audit/old-owner denial, cross-artist content/layout/media references,
completion and failure transitions across transfer, new-owner attachment/idempotent
completion, suspension preserving child states, ordered releases and safe deletion,
technical readiness, calendar/layout validation, safe public-user fields and public
optional-reference privacy. The baseline managed crypto/replay suite also passes
with the new catalog. Stage 4 records final combined and package evidence.

### Operational cutover prerequisites

This continuation targets **006_app_admin**, additive after 005. The earlier artist candidate targeted **005_artist_content**, not the prior published artifact's
002 schema. The latest pre-mission published build is
`9328bb38579e90826f5869e9595cb0639f6d05fa`, source
`715a8227d67b9805ba6508ffb3ada2fc58858400`; this is artifact history, not a claim
about the currently selected VM installation. Old packages are incompatible with
the removed artists.owner_id; this candidate is incompatible with 002-only schema.
No operational cutover, partial upgrade or auto-migration is authorized here.

The owner must review coordinated schema/package selection, canonical provenance
preparation for any existing owners (003 then genuine trusted observation, never
row-only guessed backfill) and ordered 004/005/006 application with the matching
schema006 package. Historical schema005 evidence is not a current deployment pin.
Before any live cutover, require a validated database backup and an explicitly
reviewed restore/rollback procedure, including a matching old package and schema.
004 drops `artists.owner_id`; reverting application code alone cannot restore
compatibility with the old package. The restore plan must cover the schema/data
boundary and post-backup writes; do not imply lossless automatic rollback. No
live backup, migration, restore or code rollback was performed or authorized here.
A legacy row without proof stops cutover. Empty test databases take 001..005 in order.
Primary/delegate management is now Shadify-owned. The API requires valid admitted
identity plus an active canonically observed local actor. The protected local
bootstrap seam is tested only with synthetic identities; operational bootstrap
and recovery remain unselected. No real grant was created.
New route enrollment requires Authentication/Node Agent owner review; current
authenticated managed proof does not make guest access operational.

Protected filename/interface stays unchanged: runtime owner confirms the absolute
`CREDENTIALS_DIRECTORY`; user privately places R2 values in
`storage.credentials.json` there (0600, regular file), with existing private bucket,
S3 endpoint/access_key_id/secret_access_key and independent PUT/GET TTL settings.
`database.credentials.json`, canonical profile/key/config and replay projections
remain runtime-owner assignments. No admin list or credentials enter artist DTOs.
Real R2 PUT/completion/CORS/privacy/expiry, processing, playback HTTP, guest ingress,
central account-deletion sync and VM schema/lifecycle proof remain pending. No payment,
Worker, bucket, tunnel, DNS, shared service or frontend implementation was changed.

Native GET declarations for public page and content lists use the existing
canonical shell's supported 8 MiB response bound; the other routes retain its
1 MiB default. Request field/count limits and 20-per-resource public pagination
keep responses within that bound. This is app-owned source/catalog preparation;
the owner must review exact new endpoint declarations before live enrollment.

### Stage 4 source acceptance and package evidence

After final route/privacy/response-bound changes, complete source discovery:
`env -u CREDENTIALS_DIRECTORY PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -q`
returned **72 passed** on 2026-10-03. `pip check` is clean; scoped `git diff --check`
passes. This includes 8 transactional database tests, 11 API/database tests,
page-state playback denial and the retained managed crypto/replay/provider/native
artifact tests. A former mocked SQL-string test was replaced by real isolated
database authority/SQL-binding proof; baseline behavior changes are intentional
404 private denials, membership authority and capability-aware draft DTOs.

Native candidate qualification completed for exact implementation commit
`053d93bc9b145871fae4895d6bcbadf4dd565bef`: **72 packaged tests passed** after
a fresh offline hash install of 29 production wheels; 57 manifest files, target
`005_artist_content`, packaged SQL matching tested source, clean pip check and
blocked socket/database/cloud startup-side-effect proof. Reviewed
shell0.4.6/transport0.1.9/Uvicorn0.53.0 are unchanged. Exact evidence:
`.build-tools/artist-access-offline-proof.json`; archive:
`.build-tools/shadify-api-artist-access.tar.gz`.

Archive SHA256: `5e0aae0666ef14ac776453119bed6a83ac0373456e09355d1242d6329b1d3255`.
Manifest SHA256: `fbed3ea1e39da85d08b0d09e69bc099251d63cea3316d5c05caa5024dd6b9b74`.
Documentation/acceptance-metadata corrections do not change packaged runtime,
schema, catalog, dependencies or build inputs. The existing archive/proof remain
pinned to053d93bc; a later documentation commit must not be presented as its
source. Any future exact-head artifact rebuild/publication requires separately
authorized scope; nothing was rebuilt or published for this correction.

Simplest next verification: independent source/candidate review, then replay the
isolated test commands. Any operational follow-up must separately approve exact
schema/package selection and canonical endpoint enrollment; privately supplied
R2 configuration and a tiny real upload are later integration proof, not local
test claims. The frontend documentation reference17f5f07 was published by its
owner through selector commitf18c3a6; API work did not re-push/reconstruct it.


### Historical exact readiness and audit limits (schema005 source)

`/health/ready` retains `scope=profile_database`: structural schema005 compatibility
plus the current canonical receiver, clock/replay bounds and admission/recovery
gate for user/artist-page/content metadata management. It does not prove any admin
grant or artist enrollment, a real user session, deployed guest ingress, processing,
video/profile media uploads, operational playback, purchase/payment/entitlement or
follower functionality. Storage may be missing while this scope is ready; a
configured adapter remains `configured_unverified`, never real R2 proof. Existing
original upload support still requires its own protected storage and real-cloud
verification. Video metadata management does not imply a video upload pipeline.
Self-profile editing has no endpoint. Ownership audit persists assignment,
transfer and revocation only; creation returns actor context without a persistent
creation audit event. No new audit, readiness, feature or endpoint was added by
this documentation correction.


### Historical bounded frontend capability follow-up

Parent-requested correction adds top-level admin context and create_artist
capability to the existing manageable-page response. No page-list filter or
endpoint change. A focused isolated test covers zero-page admin, zero-page
listener and revoked admin; no real admin grant was created. Qualification at
053d93bc/its 72 packaged tests predates this runtime response change: the existing
candidate does not contain it and is not a package for this later source commit.
Native build publication, installation and database preparation remain separate.

Follow-up evidence: `test_artist_api.py` focused discovery returned **12 passed**
after the capability correction; scoped diff whitespace validation passed. Its
synthetic existing-grant fixtures are confined to disposable PostgreSQL. No
operational grant/account/bootstrap tool was added. The earlier full source and
packaged 72-test evidence remains bound to053d93bc, not this later runtime change.


### Historical central-admin consumer continuation (2026-10-03; superseded)

At that historical source, central Auth architecture replaced local full-admin
grants. This intervening architecture is superseded by the 2026-10-04 app-owned
primary/delegate foundation; only its original-source evidence below is retained. No migrations were changed or auto-applied; the old table
remains inert. Page ownership, identity tombstones, suspension/publication,
verification, public privacy and immutable ownership audit remain independent.
No new endpoint, header, native catalog, dependency or protected configuration
filename is introduced. Credentials remain in the runtime-owner-confirmed protected
directory, never in this repository or chat.

Local regressions exercise exact/malformed/wrong-app claims, duplicate nested
keys, forged/unadmitted and expired requests, immutable/lost context, central
admin without a local grant, inert old grants, newly revoked admin retaining
legitimate ownership, expiry during real artist-lock waits, precommit rollback,
and both media-inspection outcomes. Retained tests cover old-owner denial,
transfer/concurrency/tombstones/publication/privacy and actual canonical ingress.
Synthetic admission, disposable PostgreSQL and stubbed R2 prove local behavior;
real Auth issuance/revocation, cloud upload, VM schema/package selection and
guest ingress remain pending. A valid previously issued claim can survive grant
revocation until expiry; this is not immediate remote revocation proof.


Final central-consumer source discovery returned **86 passed** on 2026-10-03:
`env -u CREDENTIALS_DIRECTORY PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -q`.
This includes 11 isolated transactional database tests, 16 API/database tests,
exact claim/admission tests and actual native crypto/replay ingress. Dependency
consistency and scoped whitespace checks passed. The older 72-test package
receipt remains exclusively tied to053d93bc; exact-source local qualification
for this consumer is recorded separately in .build-tools/central-admin-offline-proof.json.
No operational release, migration, central grant or real R2 acceptance follows
from these source/package checks.


### Historical bounded admin lifetime correction (2026-10-03; superseded)

Independent review accepted context/media/freshness, no local fallback and
ownership/tombstone/audit behavior, but found that the consumer accepted otherwise
valid admitted admin evidence longer than the issuer's 60-second contract. This
bounded correction enforces the lifetime at admission and context validation.
Boundary tests cover 60-second acceptance, 61/600-second rejection and unchanged
ordinary non-admin lifetimes. Previous e0be8b53 package/86-test evidence remains
historical; revised exact-source proof is stored separately in
.build-tools/central-admin-lifetime-offline-proof.json. No publication, deployment,
grant, dependency, catalog or schema change is part of this correction.


Revised source discovery: **88 passed**, clean pip check and scoped whitespace
check, 2026-10-03. The two added tests exercise admission and immutable-context
lifetime boundaries with synthetic admitted evidence; existing real native
crypto/admission tests are retained. Real Auth issuer/VM/R2 proof remains pending.


### App-owned admin foundation (2026-10-04)

1. **First proof:** isolated native PostgreSQL, explicitly provisioned synthetic
   primary, verify-only delegate, successful verification, owner assignment denied,
   revocation and next verification denied. Historical rows remain inactive.
2. **Existing operation wiring:** eight explicit capabilities above; primary-only
   grant/transfer/roster/audit routes; immutable admitted identity and additive 006.
3. **Local validation:** `test_app_admin.py` covers permission matrix through actual
   ASGI endpoints, primary transfer retain/revoke/explicit decision, delegate
   escalation denial, canonical actor/target provenance, unconfigured bootstrap,
   inactive identities, audit rollback/immutability, last-primary protection,
   simultaneous transfers and grant/operation lock ordering. `test_artist_api.py`
   retains cross-user/page privacy/IDOR, ordinary owner flows and tests revoked
   delegates during upload signing and both inspection outcomes, including
   idempotent completion. Identity tests retain audience/session/expiry/admission
   checks and prove central claims confer no permissions. Post-audit expiry rolls
   back writes, authority state and audit together.
4. **Handoff:** independent Astra review of exact local source remains pending;
   test counts and commits are recorded with the final local evidence. No package
   rebuild, push, feed publication, installation or live migration was performed.

Each app has its own independent database and access. Node deployments use the
existing central PostgreSQL cluster; WSL uses the existing WSL PostgreSQL cluster.
Development and node data remain separate. Cluster sharing does not imply shared
app tables, roles, credentials or cross-app access. Isolated tests use disposable
native PostgreSQL with a private Unix socket and no TCP listener; they neither
create live app databases nor move existing data. Exact app database/role names,
protected credentials and live migration selection remain runtime-owner decisions.

006 changes are additive and do not rewrite 001..005 or applied history. Existing
artist membership/content/ownership audit stays intact; no old full-admin grants
are activated. New source requires schema006 (readiness checks structural schema,
not presence of a primary). A source rollback alone could restore the old central
authority policy and must not be presented as a safe automatic rollback. Before
live use, select coordinated package/schema, backup/restore and endpoint enrollment,
and explicit operator bootstrap/recovery. No real account IDs or authority choices
were inferred. Central account deletion/session revocation sync remains an
Authentication/runtime integration gap; no private Auth database is queried.


Runtime table access for schema006 (app database only; no live grants issued):

| Tables | Runtime privileges used |
| --- | --- |
| shadify_users, shadify_artists, shadify_media_assets, artist_memberships | SELECT, INSERT, UPDATE |
| shadify_tracks, shadify_releases, shadify_videos, shadify_moments, shadify_events, shadify_app_admin_delegates | SELECT, INSERT, UPDATE, DELETE |
| shadify_release_tracks | SELECT, INSERT, DELETE |
| artist_ownership_audit, shadify_app_admin_audit | SELECT, INSERT only |
| shadify_app_admin | SELECT, UPDATE (also required for row locks); singleton inserted by migration owner |
| shadify_admin_grants (historical) | None |

Runtime also needs database CONNECT and schema USAGE, not schema ownership/CREATE,
DDL/TRUNCATE, sequence grants (UUID keys), authority-state DELETE or audit
UPDATE/DELETE. Migration/backup/security-maintenance ownership stays separate.
`test_documented_runtime_table_privileges_suffice_and_legacy_grants_unreadable`
uses a NOLOGIN synthetic role in disposable PostgreSQL to exercise ready,
observation, delegation, verification, artist assignment and primary transfer
with this table set; it denies reading historical grants, deleting audits/state
and truncating delegates. Parent/runtime owner must coordinate consumer manifests
and per-table grants for **006_app_admin**, not older 002/005 pins. SQL migration
logic remains solely in this API repository. Inventory found no allocated Shadify
database/role on WSL or node B; no allocation or copying app data occurred here.


### App-owned source evidence and review handoff

Runtime/schema/catalog implementation commit:
`0f280a0d06b10fcbc7a943e8228bd24e5981d3ed`, derived from the preserved original
`d6ee352c2a756ccf0e5da8b4daa72b0b48c520d7`. Isolated clone:
`/tmp/shadify-app-admin-20261004`, branch `app-owned-admin`; it has its own Git
objects/index and no source-directory watcher or linked-worktree metadata write.
The follow-up removes obsolete admin flags from synthetic identity fixtures and
records current evidence; it does not change the runtime implementation above.

Full discovery passed **105 tests** in disposable local PostgreSQL/source ASGI and
real managed shell ingress. Current `test_app_admin.py`: **18 tests**, covering the
first verify-only proof plus authority mutation, concurrency, permissions and
restricted-runtime-role checks. Tests apply unchanged 001..005 then additive006 in
order; unconfigured schema006 readiness is also true without selecting a primary.
The retained API/SQL suite proves ordinary owners, IDOR/page isolation, provenance,
membership/identity revocation, identity expiry during locks/provider work, and
public filtering. Prior central-claim tests were replaced by identity-only and
old-claim-no-power cases rather than retaining superseded lifetime expectations.

Reproduction (no external DSN, live database or cloud credentials):

```bash
cd /tmp/shadify-app-admin-20261004
env -u CREDENTIALS_DIRECTORY PYTHONDONTWRITEBYTECODE=1 /srv/Coding_space/shadify_api/.venv/bin/python -m unittest discover -s tests -q
/srv/Coding_space/shadify_api/.venv/bin/python -m pip check
git diff --check
```

Python3.14 uses the existing installed reviewed dependency environment without
installation/modification. Dependency consistency and diff whitespace checks
passed. Original checkout remains clean at d6ee352c; migrations001..005 are byte-
unchanged. Native-format tests require006/admin.py for new manifests and retain
verification of historical002/005 formats; no production archive was rebuilt.
Catalog is **54 routes**, all still canonical Authentication peers; new declarations
need coordinated enrollment at rollout. Any later issuer transition must follow
consumer selection; the source accepts identity-only assertions and old central
claims convey no power. Auth's independently reviewed identity-only forward change
is owner-coordinated; this work did not change Authentication or shared libraries.

Remaining owner choices: exact real database/role/protected projection allocation
on WSL and node; approved consumer manifest/schema006/per-table grants and endpoint
enrollment; exact deployment package; explicit real primary account, protected
operator bootstrap/recovery and coordinated backup/restore. No real authority,
credential/security setting, allocation, live migration, installation, push or
build-feed publication was made. Independent Astra review of these exact local
commits is the next source check, not a claim of deployed operation.


### Exact package qualification and coordinated rollback (2026-10-04)

Astra source PASS applies to `524e5803d3b144788d0246d18b004653dfb283b2`, including
runtime commit0f280a0d. Parent authorized isolated exact-package qualification.
Archive remains pinned to **524e5803**, even after this documentation follow-up:

- Archive: `.build-tools/app-admin-qualification/shadify-api-app-admin-524e5803.tar.gz`
- Archive SHA256: `b918e1587d41ab50f3380a41c0bc59e6e1befd6307ad1097e77243725804c559`
- Manifest SHA256: `6237734fdb274209a5ea4f50f09fa1fbfefbcf751fd1bcba8b074abb81fba980`
- Receipt: `.build-tools/app-admin-qualification/app-admin-offline-proof.json`
- Target: **006_app_admin**; 59 manifest files; 29 production wheels.

Extraction/inventory/hash validation passed; every packaged source and SQL file
matches Git524e5803 byte-for-byte. Fresh Python3.14 virtual environment installed
all production wheels offline with hash enforcement, then four existing hashed
test-only wheels. Dependency checks passed before and after test extras. All
**105 tests passed against the actual extracted packaged runtime**; the import
origin log identifies package/source, not the working checkout. Tests/tooling
come from the exact524e5803 Git snapshot; test migration files are copies of the
verified packaged SQL. Reverification after testing confirms unchanged archive
and manifest bytes. No publication or installation into an application occurred.

Auth compatibility snapshots exact reviewed
`2c94e5cac22a3e955f4e3c0119d6e05185cc252a` into the isolated proof directory.
Its real ProxyService issuer emits identity/session/assurance only, no
app_authority, with its existing 600-second identity lifetime. OTP, passkey and
`admin` assurance each admit identity without conferring Shadify admin rights.
Only explicit synthetic Shadify delegation permits verification; owner transfer
denies; revocation denies the next verification. Issuer account/session mismatch
and client authority headers fail/filter as expected; consumer account binding
and unadmitted requests deny. Three additional checks carry these actual
Auth-issued headers through the packaged canonical native ON crypto/admission
and API path. Peers, Redis commands and kernel-return inputs are synthetic;
no socket listener, real session or remote service was activated.

`compatibility-rollback.log` proves the rollback boundary in a disposable
PostgreSQL database: historicald6ee352c/schema005 code reads additive schema006,
but accepts a central admin context without reading the new Shadify primary or
delegates, and denies an identity-only context even when it is the app primary.
Current packaged source denies that central-only actor and preserves new primary,
delegate/audit state. Structural database compatibility is therefore insufficient
for safe application rollback. No destructive downgrade or migration rewrite was
added or exercised.

For any later authorized rollback, the owner must select the consumer/schema/
issuer pair together. Preserve schema006 and its authority/audit history and use
an explicitly reviewed app-owned-authority-capable consumer; do not select005
code merely because readiness passes. If no suitable pair exists, use the
existing lifecycle controls to hold affected admission/writes while choosing one,
rather than restoring central claims or reviving historical full-admin rows.
A coordinated backup/restore must cover app content, primary/delegates, audit and
post-backup writes with the matching consumer; an old backup plus old005 code is
not an automatic authority-preserving rollback. Exact operational stop/restore
selection remains owner-controlled and has not been executed.

The bootstrap seam remains internal and unexposed. **operator_reference is audit
metadata, not authorization proof.** Real protected bootstrap/recovery must verify
the separately selected operator authority; neither a caller-supplied string nor
first login selects a real primary. No operational operator/account/credential or
recovery route was chosen during qualification.

Parent/runtime coordination facts: the package manifest pins006_app_admin, the
receipt includes the exact per-table privileges exercised by the restricted role,
historical shadify_admin_grants has no runtime privileges, and both audit tables
have SELECT/INSERT only. Node Agent owns consumer manifest/per-table access,
allocation, enrollment and lifecycle; app migration SQL remains owned solely by
this repository and is shipped in the verified package. Source inventory says
node B's installed schema11 lacks the Shadify consumer and the newer prior
consumer pins002: neither is a schema006 deployment selection. No live database,
role, grant, migration, enrollment, push, deployment or build-feed action occurred.

Smallest remaining operational prerequisites: owner-approved schema006 consumer/
table grants and independent WSL/node allocations/projections; coordinated exact
package and 54-endpoint enrollment; explicit operator/primary/bootstrap/recovery
selection and backup/restore. Deploy the compatible consumer before the
identity-only issuer transition. This qualification establishes local package
behavior only; the artifact/receipt can now receive independent Astra review.


### Current-user admin summary for settings integration

Parent authorized the bounded API gap identified by Astra review01a10610.
GET `/api/me` preserves its existing user_id, account_id and artist_ids and adds
only the current user's app-owned authority summary:

```json
{"app_admin":{"is_primary":false,"permissions":["verify"],"can_manage_admin":false}}
```

permissions is the sorted effective app-admin subset from the existing locked
authorizer; it excludes independent artist-owner permissions. is_primary is
the actual current primary relation, never inferred from assurance or possessing
all eight capabilities. can_manage_admin enables the established primary-only
delegation/revocation, primary transfer and authority roster/audit actions. It is
false for delegates, ordinary users, former/revoked admins and an unconfigured
app; it never authorizes bootstrap/recovery. No primary identity, other delegate,
roster, global configuration state or revision is disclosed by this summary.

Use /api/me for current-user controls. A403 from `/api/admin/authority` remains a
permission denial and must never be interpreted as “unconfigured,” a bootstrap
invitation or a reason to fabricate an empty authority roster. Empty self
permissions likewise do not prove the app is unconfigured. Invalid/inactive
identity or unavailable authority schema remains an error; no false summary is
substituted. These responses are no-store hints, not durable authority: every
mutation reauthorizes server-side and the next self read reflects revocation.

The summary reuses the existing app-state/identity locks and expiry checks in
the /api/me transaction. No new endpoint, query framework, migration, role,
permission or table privilege is introduced. Artist ownership and ordinary
profile projection stay independent. This source change does not enter the
previously qualified524e5803 archive; that frozen schema006 package/manifest and
its105-test receipt remain unchanged for node B consumer tests. A later package
containing this additive response needs its own exact-source qualification.

Follow-up validation: **24 focused app-admin tests passed**, including six new
self-summary cases; **111 full source tests passed** with actual disposable
PostgreSQL and retained native admission coverage. Whitespace checks passed.
Frozen524e5803 archive/manifest hashes were rechecked and unchanged; no new
package, migration, catalog, dependency, grant or operational effect was made.


### Self-authority exact package qualification

Astra PASS applies to self-rights source
`9a76cacab6369dad9da87cfec4056dab2bd84296`. Parent authorized a separate local
package qualification, preserving524e5803 and the parent-controlled node B
qualification pin. The new artifact is for future UI integration, not an
automatic replacement of that pin:

- Archive: `.build-tools/self-authority-qualification/shadify-api-self-authority-9a76caca.tar.gz`
- Archive SHA256: `a16ea70254eaa3e137f3735345268f7170944ee12c3cd9a0b06d1822cb6aa471`
- Manifest SHA256: `1114f3e0a829aeb1fd21ee50ec5df4127a21633866233a578e11b3b33dfa232d`
- Receipt: `.build-tools/self-authority-qualification/self-authority-offline-proof.json`
- Target: **006_app_admin**, unchanged; 59 manifest files; 29 production wheels.

Every packaged source/SQL file matches Git9a76caca. Relative to frozen524e5803,
only access.py, admin.py and repository.py changed in the runtime inventory;
all migrations, catalog, dependencies, wheels and runtime table privileges remain
byte-for-byte unchanged. Fresh Python3.14 offline hash installation succeeded;
dependency checks are clean before and after the same hashed test extras.

**64 targeted packaged tests passed**:24 app-admin/self-summary tests,7 identity
tests,5 native managed-ingress tests,11 artist transactional SQL tests and17
artist API/media tests. The import-origin log proves execution from the extracted
package; tests/tooling come from exact9a76caca and the harness uses its verified
packaged SQL. This includes all six new /api/me cases and the existing strict
table privileges, actor/target validation, escalation denial, expiry/lock/audit
rollback, IDOR, provider revocation and canonical crypto/admission tests. The
previously run111-test full source result remains separate; no111-test packaged
PASS is claimed for this scoped qualification.

The receipt explicitly references the prior524e5803 proof and its hashes for
unchanged exact Auth2c94e5ca issuer/native compatibility and rollback behavior;
those earlier105-package-test and compatibility results remain pinned to that
older artifact. Frozen524e5803 archive/manifest hashes were reverified unchanged
after this new qualification. The parent-controlled node B qualification pin was
not changed. No publication, enrollment, live DB/grant, installation or deployment
occurred. Any future selection of the new UI-integration package remains parent/
runtime-owner coordinated; bootstrap/recovery remains internal and unselected.


### Web-owner admin completion contract and protected setup plan

This handoff is based on runtime/source9a76caca and its separately qualified
local package; documentation headba1c24e1 introduced no runtime changes. Parent
assigned only Shadify API contract/setup preparation for the four-app admin
mission. Web owner01a10381 owns UI integration. These are logical downstream API
paths: browser code must reuse the established Authentication proxy/session flow,
not call a raw receiver or supply Authentication-owned identity headers.

The exact permission union has no other roles/capabilities:

```ts
type AppPermission =
  | 'create_artist' | 'edit_profile' | 'manage_content' | 'publish_content'
  | 'change_owner' | 'suspend' | 'verify' | 'edit_user_profile';
type AccountId = string; // exact stable canonical Auth ID, 1..36 characters
type UUID = string;      // local user/page/event/resource IDs
type DelegatePolicy = 'retain' | 'revoke';
type Me = {
  user_id: UUID; account_id: AccountId; artist_ids: UUID[];
  app_admin: {is_primary: boolean; permissions: AppPermission[]; can_manage_admin: boolean};
};
type AuthorityDetails = {
  primary_account_id: AccountId; revision: number;
  delegates: {auth_account_id: AccountId; permissions: AppPermission[]; active: boolean}[];
};
type SetDelegate = {permissions: AppPermission[]};
type DelegateResult = {actor_account_id: AccountId; target_account_id: AccountId; permissions: AppPermission[]};
type TransferPrimary = {account_id: AccountId; expected_primary_account_id: AccountId; delegates: DelegatePolicy};
type TransferPrimaryResult = {primary_account_id: AccountId; delegates: DelegatePolicy};
type AppAdminAuditEvent = {
  id: UUID; actor_user_id: UUID | null; actor_account_id: AccountId | null;
  target_account_id: AccountId | null; artist_id: UUID | null;
  action: string; details: Record<string, unknown>; occurred_at: string; // ISO UTC
};
type AppAdminAudit = {events: AppAdminAuditEvent[]; next_cursor: UUID | null};
```

| Logical route | Input | Response/access |
| --- | --- | --- |
| GET /api/me | None | Me; current active canonically admitted user. No other primary/delegate disclosure. |
| GET /api/admin/authority | None | AuthorityDetails; primary only. Delegate rows can include active=false. |
| PUT /api/admin/delegates/{account_id} | SetDelegate | DelegateResult; primary only. Replace the full subset; sorted output. Empty array revokes. |
| DELETE /api/admin/delegates/{account_id} | No body | DelegateResult with permissions=[]; primary only. Revoking an inactive existing delegate is allowed. |
| PUT /api/admin/primary | TransferPrimary | TransferPrimaryResult; primary only. Incoming primary's delegate row is removed; old primary gets no automatic grant. |
| GET /api/admin/authority/audit | limit1..100(default20), cursor UUID | AppAdminAudit; primary only, opaque stable ascending-UUID pagination, not chronological ordering. |

Use is_primary/can_manage_admin for authority controls and permissions for the
eight app-operation controls. A delegate with all eight permissions is still not
primary. Artist ownership does not appear in that permission union; use artist_ids
and the selected page's capabilities for owner operations. Selected-page DTO
capabilities merge independent current ownership with the app subset. Suspension
still blocks ordinary publication; an explicit suspension operation resuming to
published also requires publish_content. Public-user profile uses the *local*
user_id UUID; grant/owner/primary targets use canonical account_id, not that UUID
or an email/display name. There is no cross-account directory endpoint.

Targets must already have active canonical-admission provenance in this same app
database. Unknown/unproven/inactive grant or transfer targets fail422 and create
no identity. Duplicate/unknown permissions, extra body fields, omitted expected
primary or omitted delegate policy fail422. An invalid/stale expected primary or
self-transfer fails409 when the actor is still primary; a former-primary actor
fails403. Account/session/admission/expiry failures remain401/503; an inactive
self projection fails404.403 is always permission denial, never an unconfigured
state. Empty self permissions likewise cannot prove configuration state.503 is
unavailable, never an empty roster. Responses are no-store and every operation
reauthorizes; refresh /api/me and applicable primary roster after success or403.

Grant writes and audit are atomic. set_delegate audit details contain before/after
permission arrays; transfer_primary details contain old_primary, the explicit
delegate policy and previous_delegates. Artist-admin mutation events contain
actual actor/action/page context; ownership assign/transfer/revoke additionally
keep the independent old/new-owner audit. Read events by the returned cursor;
occurred_at is display information, not the current page/authority state or a
cursor substitute. No audit record contains credentials or tokens. Existing
bootstrap_primary audit can have null actor fields and an operator_reference;
that reference is audit metadata, not authorization evidence or a public setup API.

#### Concrete first-primary/recovery plan; not an implemented operational route

Keep first-primary/recovery outside the ordinary browser authority controls until
the owner selects the real protected operator mechanism. The smallest app-owned
interface takes an explicit operation (`initialize_primary` or `recover_primary`),
environment (`WSL` or node B, with its exact independently assigned Shadify DB),
and exact target_account_id obtained from a successful canonical authenticated
request in that environment. Initialization also explicitly expects no current
primary. Recovery explicitly names the expected current primary and a mandatory
retain/revoke delegate policy. A non-secret operator reference/reason is for audit
only. There is no email lookup, first-user appointment, environment default,
machine-identity appointment or implicit Auth admin/assurance promotion.

Before implementation/use, the owner selects and verifies a protected operator
authorization for that exact app/environment/operation/target selection. Node
Agent owns lifecycle/DB delivery; owning a node must not itself appoint its
operator as app primary. A caller-supplied operator_reference is insufficient.
Do not manufacture an Identity/session from an account ID, load Authentication's
private DB, or turn the synthetic bootstrap helper into a browser endpoint.
The existing helper remains internal/unexposed; a real wrapper must enforce the
selected operator authorization separately and use genuine admission/provenance.

On the selected app DB, reuse app-state FOR UPDATE then sorted identity locks,
validate that target is active and canonically observed, verify the expected
primary state, and atomically set the sole primary plus append a distinct setup/
recovery audit event. Initialize only if still unconfigured; a changed expected
state fails409. Recovery replaces the expected primary without clearing/deleting
it, applies the explicit delegate decision, removes the incoming primary's
delegate row and never automatically grants the outgoing account rights. Preserve
artist ownership/content, local identity tombstones, immutable audit and migration
history. A disabled/lost primary can be recovered only by that separately
authorized operator procedure; delegates cannot recover or promote themselves.

Source acceptance for that future operational wrapper must use only disposable
DBs/synthetic operator/admission inputs: unknown/unproven/inactive identity denial,
unconfigured-only initialization, wrong environment/actor/expected state denial,
concurrent initialize/recover single winner, explicit delegate-policy handling,
audit failure rollback and no owner/content/history change. These are concrete
planned checks, not a claim that recovery is already implemented or tested. The
current verified API already covers normal primary transfer and grant management.
No real operator, target account, runtime DB, bootstrap, grant or credential was
selected by this plan. There is no additional runtime/code/package change for
this documentation handoff; qualified9a76caca and frozen524e5803 remain unchanged.

## Local media continuation (2026-10-04)

Storage is implemented before pending app-admin nomination runtime changes.
Local-first delivery uses existing app-owned manage_content/artist membership
authority; uploader remains audit only. Current private media upload/access and
completion recheck current actor/page/admin state and identity expiry. The
dedicated byte listener rechecks at final local write and on private reads.
Public derived playback DB projection enforces page/track/asset publication,
ready state, free/full or separate free preview, and denies paid full without
entitlement. It never makes uploaded originals public. No producer is added.
API receiver catalog is now 56 Authentication-only routes, adding guarded POST
media access and track playback issuance. Actual guest issuance admission is
pending Node/Auth ownership. See MEDIA_STORAGE_ARCHITECTURE.md for storage,
provider parity, migration and byte-ingress details; existing admin/owner rules,
bootstrap plan and qualified9a76 artifact remain unchanged.

Local storage exact runtime source: `7a625c1164dc7f0e26200cccced15f94309eb11f`. Separate frozen
archive SHA256 `205d64bdfc38df729105b93d5e90b3e889b67789f0d19e938400d0e8a873cb97`; manifest SHA256
`917c76d2a939bb91c051e2b549814f9a056726066b10d1492f868819069aa73e`. Fresh offline hash-pinned environment ran all
127 packaged tests, including 16 actual local storage/config
proofs. Schema001..006, production wheels and locks match the preserved9a76
artifact; its archive is unchanged. Receipt is `.build-tools/local-storage-qualification/local-storage-offline-proof.json`.
No operational guest/native byte ingress, real R2/Worker/CDN behavior, root/key
custody, installation or live activation is established by this proof.

### Astra-reviewed media correction (following frozen7a625c11)

The agreed Node-owned media socket is `/run/node-agent-shadify-media/receiver.sock`
with one small same-UID Shadify media unit and DB/storage/media-signing custody
only. Canonical GuardedASGI/catalog56 remain unchanged. Anonymous GET
`/media/v1/tracks/{UUID}/access` accepts only optional preview=true|false and
performs real DB published/free/ready/separate-preview authorization, without
synthetic identity. Originals/drafts/private/paid full without entitlement deny.
Private access/upload creation/completion remain Auth-guarded. Node owns narrow
nginx routing, HTTP1.1 streamed bodies, bounded uploads, no retries and redaction.

Streaming now performs initial authority checking and checks again before the
next chunk after two seconds (per-response clock, no shared permission cache),
while checking expiry each chunk. R2 gets absolute grant deadlines, recomputes
TTL inside the signing worker after queue delay, and validates SDK signature
expiry before returning a URL; expired grants never revive via max(1).

Focused proof:23 local/config/delivery tests,8 unchanged signing/policy tests,
and1 actual disposable nginx -> Unix Uvicorn media_runtime factory -> local/SQL
test passed. The socket fixture covers chunked PUT, completion, repeat denial,
private HEAD/Range/416, unsigned/duplicate token/wrong host/wrong method/API-route
denial, owner-transfer revocation, anonymous published-free issuance/range, and
unentitled paid denial. Its keys/identity/data/processes are synthetic/disposable,
with no TCP/public TLS or actual Node-generated projection proof. Node's exact
unit/nginx pairing remains its owner's integration task. Frozen7a625c11 archive
and9a76 baseline remain unchanged; this is a source correction, not a requalified
combined package, installation or cloud/Worker validation.

### Exact reviewed-media package qualification and Node source pairing

Exact runtime/source `63e19536bc09e075b341a75db361b73b225be95d` is frozen separately at
`.build-tools/media-review-qualification/shadify-api-media-review-63e19536.tar.gz`.
Archive SHA256 `19b13b7530075c167513b5cc642c4fcc377a7a53aefa69592b6ff139ee75aae5`; manifest SHA256
`c20f4449d8f33773d7bffc5b7c3b66fc1f19e50cd95519f9a548f012e73be720`. Manifest inventories63 files,29 production
wheels and schema target006_app_admin; no provisional nomination code/migration
is included. Every packaged source/SQL byte matches the exact commit. Only
artists.py,media_routes.py,storage.py differ from frozen7a625c11 runtime; schema,
catalog56, dependencies, wheels and locks remain unchanged. Frozen7a625c11 and
9a76caca archive/manifest hashes were checked unchanged before and after proof.

A fresh Python3.14 environment installed production and test extras offline
with required hashes, both pip checks passed, and packaged import origin was
verified.103 targeted tests passed without skips:23 local/config/delivery,
1 actual packaged nginx/Unix factory test,8 signing/policy,7 artifact verifier,
24 app-admin,7 identity,5 canonical managed ingress,11 PostgreSQL artist access
and17 ordinary artist/API tests. This is a scoped exact-package proof, not a
repeat of the older127-test full package suite. Receipt and log hashes are in
`.build-tools/media-review-qualification/media-review-offline-proof.json`.

The Node owner's source pairing is now passed, superseding the earlier pending
stage: actual generated nginx/ExecStart -> Unix Uvicorn media_runtime factory ->
exact API63 -> disposable PostgreSQL/local bytes. Its receipt/log were verified
and copied into this qualification; the receipt identifies the exact Node route/
unit/fixture hashes. It proves streamed PUT, immutable overwrite, actual app
completion, private/range/expiry/privacy checks, guest free issuance/paid denial,
transfer revocation, exact host/method/path, restart persistence, socket0660 and
redaction. UID/fixed paths/systemd custody were remapped, identities/signing were
synthetic, and completion was direct app-service invocation rather than canonical
Auth HTTP. The Node proof is source-pairing evidence reused with exact packaged
source equality; it was not rerun against a full installed Node+API bundle.

This qualification does not publish anything or select a new installation pin.
Live Node custody/systemd/public TLS/browser behavior and real R2/Worker/CDN
remain unproved. Auth nomination contracts/core are separate and unwired,
awaiting independent PASS; their branches have not been merged. No live key,
grant, media root, database migration, cloud call, push or deployment occurred.
