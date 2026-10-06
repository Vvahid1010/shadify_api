# Shadify music ingest and playback

Canonical user contract, 2026-10-06. Source: the user-confirmed text supplied at
06:43, message `Sentinel_7809d1293c308191b5bdf7f9e3ac42cc`, relayed by the parent.
These eleven sections govern staged implementation and Astra review. They are
requirements, not a claim that ingest, scanning, processing or runtime is installed.

This contract supersedes earlier media-use readiness: **an upload is untrusted
until the mandatory server pipeline approves it. No preview, playback or
publication is allowed before that approval**, including private owner/admin use.
The accepted historical `7665ec01db86fe3e01fb5678374aa7acb23ed2e7` package remains
immutable evidence of the earlier upload stage; it is not sufficient for activation
under this new requirement. App-owned artist/admin authority stays independent.

## 1. Development: Local First, R2 Ready

Store actual files on Node B local storage now, behind the storage abstraction.
Keep durable bytes outside PostgreSQL, Git and application releases. Local storage
is the current backend; R2 is a later backend without changing the domain pipeline.
Do not activate R2, a Worker, WAF or paid-service extras for this phase. Node owns
the selected runtime/root/credentials/ingress installation; this document does
not select or authorize their real values or install services.

## 2. MusicBrainz is metadata only

Use MusicBrainz for artist, album, release and track metadata, ISRC, MBID, duration
and release information. It supplies metadata for import, matching and sync;
it is not an audio source. Persist imported/matched metadata in Shadify's own
PostgreSQL so ordinary UI and playback do not depend on live MusicBrainz calls.

Shadify IDs remain the primary identities. Store external references separately
and retain provider extensibility. An MBID or another provider reference must not
replace a Track ID, artist ID or other Shadify ID. Matching/sync may update metadata
without rewriting identity. Provider API details and schema decisions belong to
their implementation stage, not an invented playback dependency.

## 3. Single and batch use the same pipeline

Support one track and batches of many tracks/albums through one ingest pipeline.
Use artist, album, title, track number, duration, ISRC, embedded metadata and
MusicBrainz results for matching. Embedded metadata is untrusted input too.
Confident matches may be automatic; ambiguous or low-confidence matches must
enter review with suggested corrections. Do not silently select an uncertain
match or create separate single/batch processing implementations.

## 4. Every upload is untrusted; server approval is mandatory

This applies to PC, Android, artist and admin uploads, both single and batch:

```text
Upload -> Quarantine -> mandatory server MalwareScan
       -> AudioValidation -> MetadataExtraction -> MusicBrainzMatch
       -> AudioProcessing -> ApprovedStorage
```

Use ClamAV or an appropriate open-source server malware scanner. Client antivirus
may be optional additional protection; it never substitutes for the server scan.
Verify actual file type, configured size, audio integrity and valid content on
the server; reject bad files. Artwork uploads also require the malware gate and
their applicable server image/type/integrity validation. Artwork is not an
exemption because it is not audio.

Quarantine is inaccessible through preview, playback, public routes and publishing.
Owner/admin permission, a valid upload token, an extension, browser MIME or a
successful byte transfer cannot bypass this gate. Missing, failed or unavailable
scan/validation evidence cannot produce approval. Approval must apply to the
actual immutable bytes used by later stages; replacing bytes invalidates that
assumption. Low-confidence matching remains in review rather than being labeled
ready. Only the approved pipeline output may enter approved storage/file use.

Do not grant an exception for previously completed `uploaded` rows. The next
implementation must fail closed when approval evidence is absent; any existing
files need an explicit staged ingest/revalidation path, not assumed approval.

## 5. Process once; preserve the master

Ingest and process once, without live transcoding during playback. Preserve the
master. Produce MP3 High at no more than 320 kbps and MP3 Low at 128 kbps, with
legacy TV compatibility. Do not imply that converting or increasing a bitrate
improves the original source quality. Record honest source/output quality;
High is bounded by source quality and the stated maximum, not an upscaling claim.
Approved original/master storage and playable processed variants remain distinct.

## 6. Player: Auto, High, Data Saver

Offer Auto, High and Data Saver and persist the listener's preference. Select
eligible ready variants according to that preference; do not run a transcode on
demand or select quarantined/unapproved/incomplete output. High uses the eligible
high variant and Data Saver the eligible low variant. Astra will specify Auto's
user-facing behavior in the existing design system without inventing a complex
streaming framework. A saved preference is not a permission or rights bypass.

## 7. Track identity is independent of assets and storage

Separate a Track from its audio assets, variants and storage/playback sources.
Support Local, R2 and future licensed sources through that boundary. A migration
or source replacement must preserve the Track ID and its references in playlists,
likes, libraries, artist/album relationships and statistics. These are preservation
requirements; they do not claim that every referenced product feature exists now.
Never persist temporary playback URLs as the identity of a track or playlist item.

## 8. Future R2 delivery follows rights and the same security pipeline

Future public/free older tracks may use stable URLs and a custom domain **only
when their rights permit public delivery**. New/protected/paid sources use temporary
URLs when protection requires them. No Worker is required or introduced here.
Do not treat track age or a free label alone as permission to expose a source.

Keep the same Shadify quarantine, mandatory scan, validation, matching, processing
and approval pipeline after adding R2. Direct uploads to R2 must first land in
separate quarantine, not immediately usable approved storage. This is a future
delivery option; no R2 bucket/domain/credentials/cost activation is authorized
by this source contract. Current local private access does not become public.

## 9. Keep the domain concepts and states distinct

Separate Catalog, Metadata, Upload, IngestJob, AudioAsset, AudioVariant,
PlaybackSource, Rights/Availability and StorageBackend. Prefer small explicit
relationships and the existing infrastructure over a generic framework or
unnecessary business rules. Exact migrations and permissions require their
own bounded stage; these concepts do not prescribe a sprawling table hierarchy.

The ingest state vocabulary is:

```text
uploaded -> scanning -> validating -> matching -> processing -> ready
                                       \-> review_required
invalid/unapproved processing outcome -> rejected
```

`uploaded` records byte transfer into quarantine, not permission to use the file.
`review_required` is an unresolved match/decision, not ready media. `ready` requires
the completed mandatory approval pipeline. Ingest state and artist/track
publication are separate: ready output still needs ordinary content authority
and rights/availability checks. Do not invent additional business restrictions
or manufacture ready rows to demonstrate playback.

## 10. Astra UX uses the existing design system

Design mobile first and support tablet, desktop and TV. Artists/admins can use
drag/drop, multiple files and folder selection where the platform supports it.
Show upload progress, scanning, processing, match suggestions/corrections, review,
source quality, High/Low readiness and Ready/Publish as useful product states.

Single upload remains simple while sharing the batch pipeline. For hundreds of
files, automatic confident matches should avoid repetitive manual work and
review should focus on exceptions. Keep correction/review state durable rather
than only transient browser IDs. Only approved files can be previewed or published.

Listeners see Auto, High and Data Saver; hide technical storage/backend details.
Do not expose keys, quarantine paths, scanner internals or temporary URLs in logs
or persistent UI state. Astra/Frontend ownership remains separate from the API
writer; this document does not authorize cross-owner UI edits.

## 11. Keep it simple, local first, low cost, secure and extensible

The goal is Local First, low cost, secure, batch friendly and R2 ready. Reuse the
storage abstraction, existing PostgreSQL/artist authority and a single pipeline.
Avoid current R2 runtime activation, Worker/WAF additions, live transcoding,
complex streaming, YouTube sources and duplicate pipelines. Future licensed
sources are an extension boundary, not a source integration to activate now.

### Gap map against accepted7665ec01

| Area | Existing source | Gap under this contract |
| --- | --- | --- |
| Local bytes and upload | LocalAdapter, StorageService, immutable PUT, size bounds and five-minute grants | Upload writes are not a separate approved quarantine workflow; scanner/approval evidence absent. |
| Completion/validation | MediaService checks length/MIME and container prefix, then marks uploaded | No malware scan, full audio integrity validation, extraction or ingest job. Prefix checks alone do not meet this contract. |
| Private use | media_routes/storage_service permit uploaded originals; draft track stores original audio/cover IDs | Mandatory approval must fence access issuance and byte reads, usable media references and file publication. Historical private-preview acceptance is superseded. |
| Artist/admin | Canonical identity, ownership, explicit app-owned capabilities and audit | Preserve these; no automatic authority or admin scan bypass. Real artist/owner activation remains unselected. |
| Metadata/matching | Manual artist/track/content data | No provider references, persisted import/sync, embedded extraction, confidence or review workflow. |
| Single/batch | Per-track upload intent and completion | No durable common ingest orchestration or batch grouping/review. |
| Audio/quality | Original WAV/FLAC; ready delivery references only in explicit synthetic fixtures | No master-to-MP3 producer, ready High/Low variants or persisted player preference/source selection. |
| Storage/source identity | Stable track/asset IDs, per-asset local/R2 provider, retained R2 adapter | Track-independent source/variant/rights model and migration-preservation proof need implementation. No R2 activation. |
| Artwork | Completed private PNG/JPEG can persist on a draft track; public delivery still gated | Its file use must also wait for server scan/validation/approval; artwork processing is not implemented. |
| Video | Video metadata and a delivery reference | Music-video upload, validation, master binding and playable processing remain a separate missing slice. This contract does not invent video ingest readiness. |
| Actual runtime | Accepted source/package and disposable proof, no active API/database/media service selected here | Approval/settings/authority and Node installation are separate from code; do not activate7665 unchanged. |

### Proposed bounded stages for parent/Astra review

1. **Contract first (this change):** preserve the eleven requirements, add owning
   pointers and this gap map. No executable, schema, dependency or runtime change.
2. **Fail-closed foundation:** review the smallest durable upload/ingest approval
   state and quarantine boundary. Fence every private/public issuance and byte
   route and file publication when approval is absent. Existing uploaded
   rows stay unapproved. Test bypass attempts, expiry/revocation, concurrency and
   crash/retry results on disposable PostgreSQL/storage before activation.
3. **One secure ingest pipeline:** review mandatory scanner composition and
   unavailable/negative-result handling, full validation, extraction, immutable
   evidence binding, master retention and once-only High/Low processing. Produce
   real test outputs, not synthetic ready rows. Node owns any later scanner/tool
   installation; none is installed by this document.
4. **Persistent matching/review and batch:** add provider references, metadata-only
   import/match/sync, confidence/review and durable single/batch orchestration.
   Define thresholds/failure handling in review, not by guessing in this contract.
   UI/playback remain independent of live provider availability.
5. **Ready playback and UX acceptance:** ready variant/source selection and saved
   Auto/High/Data Saver preference; Astra/Web implement the current-system upload,
   exception review and listening UX. Prove actual approved local audio/cover,
   refresh, wrong-user denial, interrupted/unknown outcomes and legacy TV handling.
   Parent/Node/user select and approve the exact runtime settings/roles/authority
   separately before real installation, migrations or activation.
6. **Later R2/source extension:** only when separately requested and rights-reviewed;
   retain stable identities and the same quarantine/approval pipeline, preserve
   references/data and document rollback limits. No current cloud/paid activation.

### Astra first-code-slice guidance, relayed 2026-10-06

Block both URL issuance and actual GET/HEAD/Range/audio/image preview and file
publication until valid server approval. This includes old unscanned rows and
previously issued URLs; a valid old capability is not scan evidence. Bind the
server scan record to immutable byte SHA256, scanner engine version, signature/
definition version and scan time. Absent/error/timeout/unknown scanner results
fail closed rather than being classified clean. Approved and published remain
separate. Do not invent evidence freshness thresholds or worker/role ownership
without the bounded source review.

The first shared single/batch proof should exercise real clean audio and cover,
a safe rejection fixture, all pre-scan access blocks, response-loss recovery and
persistence, and other-user denial. Keep that proof on disposable Local storage/
PostgreSQL until an exact Node B activation is separately approved. Preparing
source does not install a scanner or security service.

This is a staged proposal for Astra, not permission to install a scanner, apply
live migrations, grant accounts, activate a node/runtime or change another owner's
repository. No separate ingest implementation is shipped in this documentation
commit. Keep accepted historical source/artifacts intact and use forward commits.


### Implemented source phase1: quarantine and scanner interface (2026-10-06)

The forward009_ingest_quarantine migration adds durable jobs, immutable scan
evidence and immutable approval records. It does not modify001..008 or backfill
old assets. Completed uploads use quarantine keys and one job per asset with an
immutable SHA256. Completion and owner metadata return nested `ingest`: job_id,
state, revision, approved, retryable, last_error, legacy_unregistered. Pending
uploads have no ingest status; old completed rows have state uploaded, no job,
approved false and legacy_unregistered true. Draft metadata can retain original
audio/cover IDs. No job mutation or approval endpoint is exposed publicly.

URL issuance, actual GET/HEAD/Range and streaming, prepared playback, public
media references and media-backed publication require current approval. Old
capabilities and primary admins cannot bypass this check. Quarantine/no approval
returns409; unavailable evidence/schema returns503. Existing identity expiry,
owner access and other-user404 remain. Ordinary metadata/text flows remain usable.

The internal scanner interface claims jobs atomically using expected_revision;
one concurrent claimant wins. Evidence binds input SHA256, engine and definitions
versions, server scan time and explicit full coverage. Absent/error/timeout or
unknown/partial/skipped coverage cannot be clean. An adapter must bound execution
and full input coverage; clamscan exit0 alone is insufficient. The current
composition has no real adapter, runner, installation or automatic invocation.
Synthetic fixtures prove the interface and guards, not real malware scanning.
Clean advances only to validating; infected or changed bytes reject; scanner
infrastructure failures return uploaded with retryable true. Rejected is terminal.
A process crash can leave scanning durable and blocked; lease/reclaim is future
worker work. Lost replies reconcile durable status; stale revisions cannot replay.

No approval writer or completed ingest pipeline exists in this phase. Full audio
decode/validation, extraction, persisted match decision, processing and validated
High<=320kbps/Low128 outputs must precede approved storage. ffprobe metadata alone
is not full decode validation. Schema evidence fields reserve this boundary;
synthetic JSON markers do not demonstrate actual processing. Original input stays
quarantined; a future trusted publisher must seal approved output hash and Local
file identity, preserve masters and commit output/job evidence atomically.

Proposed role boundary for later exact Node review: API reads the three ingest
tables and inserts jobs; PostgreSQL publication row locks additionally require
UPDATE(id) on jobs, solely for FOR SHARE (identity mutation is trigger-prohibited).
The API must not receive state/revision/hash UPDATE. Scanner reads assets/jobs,
updates job state/revision/error/time columns and inserts
scan evidence; approval publishing is separately trusted and still unimplemented.
No API approval INSERT/UPDATE or scanner approval write is proposed. No real role,
grant or worker owner has been selected here. Existing artist/admin ACLs remain.

Tests use disposable PostgreSQL and Local files for HTTP access, old7665 tokens,
draft persistence, expiry before commit, other-user denial, concurrency, unknown
results and crash state. Structural synthetic approval fixtures also prove approved
GET/HEAD/Range, withdrawal of old tokens, file-identity changes and the publication
row lock; they do not demonstrate real processing. Response loss discards a successful reply; interrupted
PUT uses a TestClient generator failure, not a real network/browser fault. Historical
issuer fixture is a byte-for-byte copy of accepted7665 storage_service.py.

Native source packages now truthfully declare009_ingest_quarantine and require
the new SQL/runtime files. Historical targets remain verifiable. Accepted Node
a4a920f7 admits only through008: this009 artifact must remain offline until its
owner reviews installer, migration receipts, reader/runtime ACLs and schema
fingerprints. No live migration, scanner install, service restart, grant, push or
publication is authorized by this source receipt. Phase2 actual tools/worker,
approval/output proof and matching/playback remain outstanding.


Phase1A Astra follow-up: approval locking is now explicit and follows the owning
asset operation's lock flag. Publication keeps its job FOR SHARE lock; public
REPEATABLE READ READ ONLY projections use non-locking approval reads. Track
publishability validates delivery artwork through the same current approval gate,
so legacy or withdrawn covers hide the track and block publication. Disposable
PostgreSQL HTTP tests positively preserve approved audio, artwork/avatar/header
public references and reject withdrawn/legacy covers. These remain structural
fixtures, not a real malware/decode/processing acceptance claim.
