# Migration identity correction and exact security delta

Parent has received the previous handoff; no cross-thread message or user
coordination question is needed. Shared-file integration stays with its owners.

Read-only inspection found active service WorkingDirectory:
`/var/lib/chapary-local/shadify-dev/package-public-artists-v1/source`. Its SQL
inventory contains `013_release_suggestions.sql` and `014_artist_profile_media.sql`.
The development checkout is missing these historical identities. The original
unexecuted `014_artist_managers.sql` proposal is therefore renamed to
`015_artist_managers.sql`, the next presently unoccupied identity after014. No
historical SQL was edited. The media worker's proposed013_ingest_video also
collides; parent must assign it a distinct subsequent identity (016 if015 remains
reserved for managers), not replay or relabel applied013/014 files.

## Runner identity/order

App runtime explicitly performs no migrations. Inspected Node owner runner
`/srv/Coding_space/node_agent/backend/node_agent_mizfood_helper/shadify_migrations.py`
uses exact TARGET002, exact FILES001/002 and pre/post schema-inventory hashes; it
does not glob or infer order from numeric prefixes. Source release builder
currently advertises target012; verifier accepts only explicitly listed historical
targets. Neither automatically supports015. Active WSL package SQL inventory
proves a filename identity collision; this is not a read of the live applied ledger
or committed migration hashes. No DB credentials/ledger were read. WSL activation
owner must review actual schema/receipts and exact migration chain before apply.

## Table/privilege delta

Only new relation: `artist_manager_grants(artist_id uuid, user_id uuid,
permissions text[])`. Composite PK(artist_id,user_id); user/artist FKs with ON DELETE
RESTRICT; CHECK requires a nonempty subset of edit_profile/manage_content/
publish_content; index(user_id,artist_id). No new DB role/function/trigger, no RLS
change, no owner/primary/global-delegate/ingest/profile-image table alteration and
no identity or grant seed.

Proposed runtime privilege delta: SELECT,INSERT,UPDATE,DELETE on the new table for
the existing Shadify runtime role in its reviewed schema. Exact role/schema must
be confirmed by deployment owner. No role creation/administration, schema DDL,
security-definer function or bypass privilege is required. Existing artist/app/
identity locks, authority revision UPDATE and app admin audit INSERT are reused;
no new privileges on those relations. No GRANT statement was run or embedded with
a guessed runtime role.

## Authority functions

Integration patch affects ArtistAccessRepository composition and _authority
(artist-specific permission union under its existing lock),
ArtistOperations.manageable_pages (scoped authorized search) and
install_artist_routes (new independent route registration/search). No change to
admin.py, owner-change logic, media/scan gates, primary/global-delegate management,
ordinary primary transfer or runtime/security configuration.

Independent mixin adds _artist_manager_schema, _artist_manager_permissions,
artist_managers, set_artist_manager and accessible_artist_pages. New roster and
PUT/DELETE require primary. Scoped grants contain only three ordinary artist
capabilities, cannot affect other artists and never become global grants.

## Rollback constraints

Use a separately reviewed operational procedure. Before source rollback stop new
scoped-manager mutations and finish inflight transactions; old source ignores
scoped grants, removing their management access while preserving owners/global
admins. Preserve the new table and append-only audit history unless a specific
archival/drop plan is approved; dropping it destroys scoped grant state. Never
roll back013/014, delete identities/content, decrement or reuse authority revision.
Source rollback cannot undo already committed edits/publication. Migration has
no down/drop step. Lost migration replies require schema/receipt inspection,
not automatic replay (CREATE TABLE intentionally fails if already present).

## Verified fingerprints and primary-admin reuse

Source-only015 SQL SHA256:
9c11c0b0140d9add1b413fe74ff3898274235e0e43eba03ee8235ec6ed841f7f
Active-package013_release_suggestions SQL SHA256:
5fa0f44a20f1fc4e55cca254f67c4f18d20f984cb3665b4411bd2f9b6dce5549
Active-package014_artist_profile_media SQL SHA256:
92c11339e4715febdbbbb7a6474a5c7b80ffe1f9ced79c242130f58020870620
These are on-disk SQL fingerprints, not assertions of applied DB ledger hashes.

Existing primary-admin projection is GET /api/me -> app_admin.is_primary and
app_admin.can_manage_admin. Both originate from repository.project_user at
shadify_api/repository.py:108-116, which calls locked server authorization;
can_manage_admin equals primary, never a complete delegated permission bundle.
Web src/domain/admin.ts projectAdminSelf enforces that equality and validates
account_id. src/stores/admin.ts canManage additionally requires ready state,
both booleans true and primary_account_id from fresh primary-only roster matching
its actor. There is no existing exported isPrimaryAdmin helper; reuse the validated
self/actor contract, or its existing canManage guard when that roster is loaded.

Server primary-only actions must call the existing admin._primary_actor with the
locked app-state and fresh admitted identity, or authorized(admin_only=True) with
no ordinary operation. All existing app operation permissions do not pass that
primary check. Do not use publish_content to authorize global Discover publication.
The scoped artist grant table cannot carry Discover edit/approval/publication and
none of its reads are included in /api/me's global permissions projection.

Final user Discover rule supplied by parent: separate Discover editor prepares
content/order and submits; primary alone approves/public-publishes every version.
Same Discover admin page may show primary-only delegation controls via the above
identity guard. This feature is owned by the separate Discover worker; no Discover
capability or grant is added by this task.

Isolated scoped-manager tests apply001..006 plus015 to prove the additive access
change; they do not certify the full live001..014 migration chain or its runner.
