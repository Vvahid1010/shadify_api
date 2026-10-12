> Historical source-preparation checkpoint. The pending integration and
> source-only statements below describe that earlier checkpoint, not current
> runtime status or a new work order. Consult current source and deployment
> receipts for present behavior; this document grants no runtime authority.

# Artist Studio role/list and scoped-manager source handoff

User approved the role/list/per-artist approach. This task writes independent
source files only. Shared-file coordination is pending; the cross-thread message
was rejected by automatic approval review. No operational database migration,
real account/grant, credential, ingress/security enrollment, push or deployment.

## Files prepared in shared source

API:
- `shadify_api/artist_managers.py`: optional scoped-grant reader; primary-only
  roster/replacement/revocation; fresh identity, artist/app/identity lock order,
  app revision CAS and transactional append-only admin audit. Default recommended
  subset is edit_profile/manage_content, publish_content remains explicit.
- `shadify_api/artist_manager_routes.py`: independently installable GET roster and
  PUT/DELETE manager routes under `/api/admin/artists/{artist_id}/managers`.
- `migrations/015_artist_managers.sql`: additive composite artist/user grants,
  only edit_profile/manage_content/publish_content; no seed or owner backfill.
- `tests/test_artist_managers.py`: disposable PostgreSQL grant/list/CAS/identity/
  ownership/global-delegate/lock-wait proof, synthetic accounts only.

Web:
- `src/components/artist-studio/StudioArtistList.vue`: compact searchable rows,
  status/verification/owner/manager relation, create, pagination, loading/empty/error.
- `src/components/artist-studio/StudioArtistManagers.vue`: primary-only scoped
  roster/form, exact recipient/artist/actor/before/proposed review, explicit
  confirmation, no stale/conflict/unknown-outcome mutation retry; account/page
  generations discard stale responses. Existing canonical account ID only.
- `src/providers/api/artistManagers.ts`: same-origin/no-store cookie transport,
  response actor/artist/target/revision/permission checks, no token persistence.
- `src/domain/studioLanding.ts`: authoritative single-owner/direct-link/list
  choice; explicit list return blocks auto-entry; older/unknown DTO fails to list.
- `tests/studio-landing.test.ts`, `tests/artist-managers.test.ts`.

## Shared API hooks (owner action required)

Review the exact `artist-manager-integration.patch` in the Windows task workspace.
It touches only access.py, artists.py, artist_routes.py, not main/security/runtime.
The production candidate composes ArtistManagersRepository into access, merges
scoped permissions only for a specific artist AND configured primary under the
existing app-state SHARE lock, delegates manageable_pages to scoped union/search,
and installs the independent routes using the existing admitted identity dependency.
All scoped admin-only operations outside the three allowlisted permissions still
deny. Existing global delegates remain global; none are silently converted.

`GET /api/me/artists?limit=100&cursor=...&search=...` supplies existing fields plus:
- `landing_mode`: list for primary, any global delegate, any scoped manager,
  multi-owner or no owner; single_owner only for non-manager with exactly one owner.
- `sole_owned_artist_id`, `is_primary`.
- per row `is_owner`, `is_artist_manager`; effective existing per-page capabilities.
- top-level capabilities `create_artist`, `manage_artist_managers` (primary only).
Search is a literal case-insensitive name/slug substring applied AFTER authorization
and BEFORE cursor/limit. Owner count/landing is independent of search/pagination.
Missing migration supplies no scoped authority; scoped-manager operations return503.
Before deployment review schema migration plus exact runtime-role table privileges
and existing endpoint enrollment. This task does not apply any of these changes.

## Shared web hooks (owners action required)

StudioWorkspace owner:
1. Read listing before implicit entry; call studioEntryArtist with current artist
   query and explicit-list marker (`view=artists` suggested). Use router.push to open
   a list row, retaining a list history entry; only sole_owner may auto-replace.
2. Mount StudioArtistList when no artist selected. Explicit back/list removes artist,
   tab/panel query, sets view=artists, and invalidates selected detail/content/preview
   request generations so in-flight responses cannot reopen an artist. Preserve
   per-page unsaved drafts when still authorized; deny/logout continues existing wipe.
3. Query/search changes reset pagination and invalidate older list responses; debounce
   search and call the existing store/provider, not a page-owned fixture filter. On
   entering list from a singer workspace, owner authoritative sole id remains known
   even if a search has no results. Avoid selecting the first visible result.
4. Render StudioArtistManagers only when listing.is_primary and authenticated API
   mode; pass selected artist/actor. It is a child of the existing URL/focus-safe
   manager sheet. Source preparation does not authorize a real recipient grant.

Store/provider/domain owners:
- Add additive optional role/list metadata to PageList/PageSummary, leaving required
  availability and existing fields intact; extend pages(cursor?,search?) transport.
- Add search/list-loading/page-return handling through the existing scoped store.
- Reset private selected state plus pending request generations on explicit list;
  existing late-response, denial, account-change and persistent-player rules remain.
- Do not derive privilege from email, counts, browser labels or ownership alone.

## Validation and limits

Initial shared independent SQL suite: 8 passed. Web helper/provider: 7 passed.
Isolated integrated API candidate: 12 scoped tests (including inherited eight),
real PostgreSQL and HTTP, direct other-artist denial, publication separation,
suspension gating, revoke then direct-link denial and concurrent revoke/commit.
Legacy global app-admin suite: 24 passed on the isolated candidate.
Scoped vue-tsc and ESLint pass. Full web typecheck hits pre-existing binary `.ts`
video segments in public/tv/debug-media; full diff-check reports unrelated TV CRLF.
No browser proof of a mounted Studio landing/list yet because shared workspace
hooks remain with their owner. Do not call this feature live/fully connected.

Candidate location: `/var/tmp/shadify-role-candidate-cawdlWe1`. `/tmp` tmpfs is full;
tests use TMPDIR=/var/tmp, no other worker's files or services are removed.
Integration test source is in the Windows task workspace and candidate only,
`test_artist_manager_integration.py`; copy into shared tests after hook review.

## Discover boundary

No global Discover permission is added. Recommend a separate explicit Discover
editor grant for selected rails or the whole Discover surface, granted/revoked
only by primary, independent of artist-manager and publish_content. Artist managers
could submit their own artist's eligible published resources for review; final
rail placement remains a separate curated operation. Parent resolves the product
choice before implementation; no automatic mood/publication inclusion follows.


Migration identity corrected to 015; see MIGRATION_IDENTITY_CORRECTION.md. Parent received this handoff; the earlier messaging/coordination question is superseded and no further message is needed.
