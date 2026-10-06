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


### Source phase1B: bounded scanner and native worker (2026-10-06)

`python -m shadify_api.ingest_worker --config <absolute protected JSON>` is the
owning standalone entry. It loads only explicitly selected protected worker DB
credentials and Local root; it creates no listener, Auth identity, migration,
role, directory, scanner install or signature update. Select a non-root app OS
identity, separate least-privilege DB credentials and existing private state/temp
directories. LocalAdapter currently requires the media root's app UID; Node should
restrict the worker's filesystem access with ReadOnlyPaths for media/definitions,
only private worker state/temp writable, no network except its assigned DB.
No app-media signing key or Auth/replay credentials are needed by this entry.

Additive010_ingest_attempts preserves001..009. Attempts record job/input SHA256,
claimed revision, server start/expiry, and a once-only completed/abandoned outcome.
Claim, expired-attempt abandonment and next claim are atomic; scan evidence,
completion and resulting job state commit together. A stale/expired result cannot
commit. Committed reply loss cannot rescan a validating/rejected job. Unknown
scanner infrastructure returns uploaded with a bounded retry delay. Unleased
historical scanning jobs are not guessed into automatic recovery. There is no
heartbeat: a fixed lease exceeds the bounded scan deadline by at least120seconds.

Concurrency is exactly one on selected Node B. Worker holds a private flock for
its lifetime and inherits it into scanner subprocesses; database session advisory
exclusion additionally covers each complete claim/scan/commit. Expiration alone
cannot start a parallel scanner while the first session is alive. An orphan child
retains the host lock until exit; Node's unit packet must kill the whole control
group on service termination/crash and bound restart/stop handling. Multi-node
worker deployment is not admitted by this source contract.

ClamAVScanner requires an explicit root-owned Bubblewrap executable for EVERY
invocation, including --version. There is no bare scanner fallback. Node prepares
a sealed root-owned minimal native ELF closure: /usr/bin/clamscan and only its
loader/shared-library dependencies, optionally ld.so.cache. The API validates its
bounded inventory/hash and the selected scanner's binary hash; no arbitrary host
/usr, /etc, home, app, media, socket or credential directory may be mounted.
The fixed command unshares user/PID/network/IPC/UTS/cgroup namespaces, disables
nested user namespaces, drops capabilities, clears the environment, mounts that
closure read-only, creates its own /proc and /dev, mounts only the selected main/daily/bytecode definition FILES read-only and
uses a private size-limited tmpfs /scratch. It never binds the whole updater
directory, freshclam state or ignored temporary files. All descendants share this boundary.
Node must qualify Bubblewrap/kernel/AppArmor compatibility on actual B; failure
is unknown/unapproved, never an instruction to relax isolation or use a fallback.

The parent feeds admitted bytes over stdin PIPE; the original asset descriptor,
worker database descriptors and ambient inheritable descriptors are not passed.
Only stdio and the non-secret host lock descriptor enter Bubblewrap. --sync-fd
retains that lock in namespace PID1 and closes it before payload exec. Tests prove
payload and descendants cannot read synthetic DB/app credentials, host parent
/proc environment/descriptors, an ambient inheritable secret descriptor or a host
loopback socket; they cannot write runtime/definitions. Lock exclusion persists
until namespace teardown. Timeout/interrupt kill the outer process group;
--die-with-parent and PID namespace teardown remove detached descendants. Node's
unit must still use KillMode=control-group and qualify crash/stop/restart cleanup.

The selected Ubuntu package is 1.5.4+dfsg-0ubuntu0.26.04.1; this source accepts only
the exact engine protocol 1.5.4. Its upstream stdin path counts a full clean scan
as two Scanned files, infection as one; trusted-shortcut OK with one is unknown.
--log-hash --hash-alg=sha2-256 requests the computed original-input SHA256, never
a hash hint. The report must contain exactly that admitted hash, positive known
signatures, zero diagnostics/errors, the exact version and expected full outcome.
Exit0 alone, missing hash, skipped/partial/trusted input, warnings, heuristic
size/encryption/coverage alerts and changed definitions remain unknown. Definition
fingerprints cover main/daily/optional bytecode versions and full byte hashes.
No clean verdict approves media: clean advances only to validating.

Protected JSON fields: database_file, local_root, state_dir, temp_dir, clamscan,
prlimit, bwrap, scanner_runtime_root, scanner_runtime_sha256, definitions_dir,
engine_version, limits. Runtime/executable ancestors must also be root owned and
non-writable. Config/state/temp/media/definitions are outside the scanner closure.
Limits propose input100MiB, expanded/scratch200MiB, wall/CPU180seconds,
memory2GiB per process, output32KiB, lease360seconds, retry30seconds, poll5seconds.
2GiB IS NOT qualified sufficient with real signatures. Node must measure actual
definitions and review the whole-unit MemoryMax/TasksMax plus size-bounded scratch
before activation; per-process prlimit alone is not an aggregate descendant cap.
No service, cgroup/security setting, package or directory is installed by source.

Exact proposed worker DB privileges, qualified with a disposable NOLOGIN role:
assets SELECT; jobs SELECT and UPDATE(state,revision,last_error,updated_at); scans
SELECT,INSERT; attempts SELECT,INSERT and UPDATE(finished_at,outcome); schema USAGE
and normal advisory-lock functions. No approval/artist/admin table access or scan
deletion. API privileges and approved read/publication checks remain unchanged;
the API does not need attempt-table privileges. This document grants no real role.

Tests use actual subprocess groups/prlimit, disposable PG and synthetic clamscan
protocol/definition files. They cover partial/limit/diagnostic/version/change/
timeout/overflow denial, child termination, inherited host lock, real worker
process SIGKILL/restart, leases, active-worker exclusion after expiry, stale-result
denial, transaction failure and minimal privileges. No clamscan/freshclam/ffmpeg/
ffprobe exists on this WSL PATH, so no real AV/signature or audio-processing
acceptance is claimed. Exact Node010 installer/SQL receipts/ACL/fingerprint proof
and actual tooling/settings/authority activation remain outstanding.

Upstream behavior references: [ClamAV scanning options](https://docs.clamav.net/manual/Usage/Scanning.html)
describe oversized-file skipping; the [1.5.4 stdin implementation](https://github.com/Cisco-Talos/clamav/blob/clamav-1.5.4/clamscan/manager.c)
and [CLI implementation](https://github.com/Cisco-Talos/clamav/blob/clamav-1.5.4/clamscan/clamscan.c)
ground the strict protocol and explicit coverage controls. Future actual-tool
proof must confirm the selected Ubuntu package's behavior with safe fixtures.


Worker bound tightening: pre/post input hashing shares the scanner's wall deadline
and enforces the admitted byte count, detecting growth before reading more than
one extra byte. Oversized metadata is rejected before hashing/scanning. Definition
hashes also cannot read beyond their initial admitted size. Worker DB composition
requires one explicit numeric endpoint (localhost maps to127.0.0.1) and supplies
hostaddr with existing5s connect/10s statement/5s lock bounds, avoiding unbounded
DNS resolution. Tests cover growth, oversize and already-expired shared deadlines.


Each invocation creates only a temporary empty host workspace placeholder; scanner
scratch bytes exist in its private bounded tmpfs and disappear on namespace
teardown, including SIGKILL. Normal parent cleanup removes the empty placeholder;
Node may clear abandoned placeholders only after whole-unit shutdown. Protected
roots are never provisioned or guessed. Historical unsandboxed010 artifacts remain
preserved for evidence but are superseded for activation; new010 package validation
requires scanner_sandbox.py. SQL001..010 bytes and Web DTOs are unchanged.

Session exclusion uses the resolved shadify_ingest_jobs relation identity and
database, rather than current_schema: different leading search-path schemas still
share the same lock when they resolve to the same jobs. A real PG test keeps the
first scan session active after expiry and proves the alternate-search-path worker
cannot reclaim/process in parallel.


Astra fairness correction: eligible claims order by updated_at, created_at, id.
A timeout or reclaimed attempt advances updated_at, sending that job behind older
waiting work rather than preserving permanent priority by original creation time.
Retry eligibility and lease/advisory fencing are unchanged. A real PG regression
scales180s/30s to60ms/10ms, queues freshC after oldA's timeout and proves repeated
oldA/oldB timeouts produceA,B,A,C, giving the fresh job a turn. No migration,
DTO, entry, privileges, paths or resource setting changes are required.

Sandbox source qualification uses real Bubblewrap0.11.1/prlimit and a disposable
Python reporter/ELF fixture closure; this fixture is deliberately rejected by the
production root-owned native-closure validator. It is not an AV engine or signature
acceptance test. Official source protocol review and synthetic format tests are
complete; actual selected package/signatures/RSS and Node010 integration remain
open pending the parent's package-install authorization and Node ownership.

### Next bounded slice: validation, matching and approved outputs

Source plan after accepted440b9c64, not runtime or migration activation. Keep one
ingest worker/queue, its resolved-job-table advisory lock, host flock, immutable
input hashes, expiring revision-fenced attempts and mandatory tool namespaces.
The first useful case is a short real WAV/FLAC plus PNG/JPEG track cover. Larger
catalog imports, provider sync, batch optimization, playback preference UI and
R2 remain queued. No first-user grant, UI edit or independent artist authority.

**One additive schema target:011_ingest_processing.** Preserve001..010 bytes and
all history. Extend010 attempts with an immutable stage(scan,validation,matching,
processing), assigning existing attempts scan because010 only ran scans. Extend
the existing job error CHECK for validation_invalid,metadata_invalid,
provider_unavailable,match_ambiguous,processing_unavailable,processing_timeout,
processing_error,output_invalid,pipeline_evidence_invalid. Keep eight existing
states. Tool/provider infrastructure errors remain retryable; malformed content
rejects; actual ambiguous/empty matching results require explicit review.

Three small app-owned tables, with append-only evidence and no deletion:

- shadify_ingest_stages: id UUID PK,job_id,input_sha256,scan_id,stage
  (validation,extraction,matching,prepared_outputs,processing),payload JSONB,recorded_at server time;
  UNIQUE(job_id,stage),FK(job_id,input_sha256) to jobs and a composite FK to the
  exact full clean scan. Payloads have strict bounded server DTOs. Matching payload
  records request/snapshot SHA256,provider=musicbrainz,fetched_at,complete response
  and at most five candidate recordings with MBID,normalized metadata and score.
  prepared_outputs pins tool/pipeline versions, deterministic role IDs/keys and
  actual encoded output SHA256/size before any install; processing records verified
  installed identities and refers to that prepared manifest.
- shadify_ingest_match_decisions: id UUID PK,job_id UNIQUE,input_sha256,
  matching_stage_id,method(automatic,reviewed),selected_recording_mbid nullable,
  corrected_metadata JSONB,reason,actor_user_id nullable,decided_at server time.
  The selected MBID must belong to the recorded candidate set. Automatic requires
  a unique exact ISRC plus consistent title/artist/duration; the confident automatic path is required in this slice. Explicit reviewed no-match requires a completed real
  lookup snapshot and reason; unavailable provider is never a no-match decision.
  Artwork records a reference to the resolved audio decision for the SAME track
  and artist, with that referenced audio job ready before artwork approval.
- shadify_ingest_variants: job_id,role(master,high,low,cover),asset_id UNIQUE,
  output_sha256,source_quality JSONB,pipeline_version;PK(job_id,role). Asset rows
  use existing media types/providers/access rules; roles do not replace Track IDs.

Existing009 approvals remain the approval record. Add forward evidence constraints
requiring the immutable clean scan, passed validation, complete extraction,
resolved recorded matching and successful processing for the SAME job/input;
audio needs master+high+low and artwork needs master+cover. No stage alone can
produce ready. Output rows, variants, all approvals, processing evidence, attempt
finish and ready transition commit in ONE transaction under job/attempt locks.
Current/expired attempt checks apply immediately before commit. No public caller
can write stage evidence, processing manifests or approvals.

**API contract:** keep GET /api/media/{asset_id} and its existing ingest DTO; add
outputs:[{role,asset_id,mime,size}] only for ready approved outputs, never keys or
URLs. Original quarantine source remains inaccessible even when its job is ready;
its current approved boolean still describes that asset, not every output. An
approved master is a separate immutable copy. This intentionally retains the
quarantine source for audit/recovery; safe later retention/GC is separately scoped.
No automatic overwrite of user draft metadata or Track asset references: approved
IDs can be selected through existing authorized content edits.

GET /api/media/{asset_id}/match-review uses existing read_content authority and
returns job/revision,bounded persisted extracted metadata,lookup status,candidate
IDs/metadata and the recorded decision. PUT on that path uses existing
manage_content authority with expected_revision and a strict body: selection
(recorded candidate MBID or explicit no_match),reason and bounded corrected title,
artist,album,track_number,ISRC fields. It can resolve only review_required with a
completed lookup; it inserts an immutable actor/reason decision audit ONLY (there is no general content-audit
table); existing authorized admin audit remains independent.
The worker claims that decision and performs the fenced processing transition;
the API has no general ingest-state UPDATE privilege. It cannot approve, publish, change artist/track IDs or submit raw
provider evidence. Foreign page/track denies; stale revision409; expiry401/403.
Artist -> app authority -> sorted identities -> track -> job locking and actor
rechecks before audit/commit preserve revocation/account-deactivation separation.

**Worker implementation order:**
1. Claim validating using the same attempt table, preserving state and incrementing
   revision. Full probe+decode verifies actual WAV/FLAC codec, exactly one audio
   stream, bounded channels/rate/duration and no unexpected stream; images require
   supported PNG/JPEG, bounded dimensions, exactly one complete decoded frame and
   no unsupported animation/extra payload. Signature/browser MIME alone cannot
   pass. Extract bounded plain-text tags and honest codec/sample/bit-depth/source
   quality; embedded tags remain suggestions, never authority. Commit immutable
   validation+extraction evidence and matching state atomically.
2. Matching persists a server provider snapshot before deciding. Public metadata-only MusicBrainz queries are authorized: fixed HTTPS host,
   meaningful User-Agent, maximum one request/second, bounded deadline/body,
   no credentials/audio/keys/account data and no redirects. Injected deterministic snapshots
   can exercise offline tests, explicitly labelled fixtures. Production without
   a qualified provider stays matching/provider_unavailable and cannot approve.
   Real ambiguous/empty results move to review_required. Covers associate with
   that track's resolved audio decision instead of pretending to be recordings.
3. Processing uses fixed FFmpeg commands with metadata stripping, explicit stream
   mapping, no network protocols or user argv, pinned encoder/version/settings,
   deterministic role IDs/keys and no on-demand transcode. Preserve master bytes;
   produce MPEG-1-compatible MP3 High<=320kbps and Low128kbps,44.1kHz,at most two
   channels. Do not claim source improvement. Normalize a single-frame cover to
   PNG. Probe AND fully decode each resulting output, verify bitrate/duration,
   channel/rate/dimensions and hash. Encoder exit0 alone is not validation.
4. Persist the append-only prepared_outputs manifest after actual encode/hash and
   before install, then stage bytes through LocalAdapter's exclusive install outside quarantine,
   fsync and verify actual file identity/hash before the final DB transaction.
   Until approvals commit, installed bytes have no usable asset grant. Use stable
   job/role/pipeline keys and accept existing outputs only after exact byte checks.
   Crash after file install leaves inaccessible candidates bound to the persisted
   prepared manifest; restart reuses only exact hash/size/pipeline matches and
   never overwrites immutable keys. A crash before install may require regenerating
   missing output bytes and checking they match that same deterministic manifest. Successful
   ready jobs do not encode again; lost commit replies recover from DB evidence.

**Node changes sent BEFORE011 freeze:** same native unit, concurrency1 and existing
scanner contract unchanged. Add a separate minimal sealed root-owned ELF closure
containing only ffmpeg,ffprobe and exact dependencies, explicit version and closure
hash fields; do not widen scanner closure or mount app/DB credentials. Every media
tool preflight/probe/decode/encode uses fixed Bubblewrap namespaces, private bounded
scratch, PIPE input/output, close_fds and PID1-held nonsecret lock. No bare fallback.
Measure actual codec/definition RAM and aggregate whole-unit MemoryMax/TasksMax;
2GiB remains unqualified. Limits and output-volume budgets are fixed before final
source/package proof, not guessed into an installation command. Pipeline writer
needs only new stage/decision/variant INSERT/read and derived-asset/approval writes
in addition to current worker privileges; API gains review SELECT/decision INSERT,
never scan/validation/processing/approval write. Exact grants require disposable
least-privilege qualification and independent Node review, no live grant here.

**Required proof:** short safe real audio+cover through mandatory scan, full decode,
extraction, recorded matching/review, deterministic High/Low and cover validation,
then atomic approval; truncated/corrupt/type-mismatch/oversized/multistream inputs
never ready. Test low-confidence/provider failure,no-match review,foreign candidate,
other user/page,stale editor,revocation/expiry while waiting,concurrent attempts,
expired result,crash between installs/DB commit,lost reply,unchanged master and
repeat hashes/no re-encode. Real fixture bytes generated locally, no catalog/audio
downloads. Trusted test snapshots and synthetic tool/scan protocols are NOT real
provider/AV/encode proof. Tools currently absent here; installation remains with
the parent/Node owner and requires its own existing authorization. Public metadata lookups are permitted; no runtime activation, SQL application,
role/grant, package feed or cloud activation.

Official tool contracts: [FFmpeg](https://ffmpeg.org/ffmpeg.html),
[ffprobe](https://ffmpeg.org/ffprobe.html),
[MusicBrainz metadata API](https://musicbrainz.org/doc/MusicBrainz_API).

Implementation correction: transient provider failures write no matching stage
or decision and therefore do not occupy its unique success-evidence slot. The
worker records a retryable job error and retries with a fresh bounded attempt.
Cover matching evidence is job/input/scan-specific and records an explicit foreign
key to the resolved audio decision for the same artist AND Track ID. Tools have
no network; only the bounded public-metadata provider child may reach HTTPS/DNS.
MusicBrainz requires meaningful application identification and at most one request
per second: [official API requirements](https://musicbrainz.org/doc/MusicBrainz_API#Application_rate_limiting_and_identification).

### Implemented bounded011 source (2026-10-06)

One additive011_ingest_processing now implements that plan. API review inserts
decisions only; PipelineWorker observes them and claims processing under the
existing table/session/host exclusion and durable attempt lease. Successful
stage/decision/variant rows are append-only. Unknown provider results consume no
matching slot. Covers keep their own clean-scan/validation/extraction/processing
evidence plus a same-artist/same-track audio-decision FK, locked/rechecked at
promotion. Prepared manifests pin actual encoded hashes/role IDs before install;
ready is a single DB commit with ALL required approvals. Master hash/mime/size
and owner/artist/track identity are checked by forward approval constraints.
Source SQL001..010 is preserved byte-for-byte; no real schema is applied.

Media commands use a separate sealed native ffmpeg/ffprobe closure, never a wider
scanner image. Version preflight, probe, full decode and encode all run in the
same fixed native namespaces/PIPE boundary. Audio initially supports integer PCM
WAV and FLAC, one stream,1..2channels,8..96kHz,positive duration<=1hour and decoded
bytes<=200MiB. Unsupported WAV compressed/float codecs reject honestly. Images
support one PNG/JPEG frame<=4096x4096, strict PNG CRC/end/no-animation and JPEG end,
full decoded pixel count and a normalized validated PNG output. Formats/codecs,
actual input bounds and full decode are checked, not extensions or exit status.
Encoded outputs are metadata-free MPEG-1 Layer III44.1kHz stereo,CBR320/128kbps;
every frame header and full probe/decode/duration are checked. No source-quality
improvement is claimed; the master remains byte-identical and quality is recorded.
These small-case bounds are explicit, not a full catalog/media platform promise.

MusicBrainz is public metadata only: validated ISRC lookup or escaped title/artist
recording search on the fixed HTTPS endpoint. Meaningful configured version/contact
User-Agent, no credentials/proxies/redirects,1MiB HTTP body cap,64KiB normalized
output and12second parent-enforced deadline including DNS/slow reads. A private
boot-ID/monotonic rate stamp survives restarts, with1.1seconds between starts;
only one selected B worker/provider is admitted. Missing/invalid/429/5xx results
retry without matching evidence. Completed ISRC404 Not Found can become empty
review suggestions; it never automatically approves. Tool namespaces cannot use
the provider's network. A successful source-client public metadata request using
an official example ISRC completed here; no audio, account fields or credentials
were sent. That proves transport/protocol, not a match for user's audio.

011 API role adds SELECT stages/decisions/variants plus INSERT decisions; existing
job UPDATE(id) supports row locking, never state/revision writes. Pipeline worker
adds SELECT/INSERT stages/decisions/variants/approvals/derived media,SELECT(id,name)
artists and SELECT(id,artist_id,title) tracks; it has NO artist/admin mutation or
identity-table access. Derived INSERT FK scope locks precede the final job lock,
avoiding actor lock inversion without granting artist UPDATE. Existing job-state,
scan and attempt privileges remain narrow. Roles are qualified only with private
NOLOGIN fixtures; no grant/install/runtime decision follows from this document.

Tests exercise real PG/HTTP/local objects and real Bubblewrap/prlimit with labelled
synthetic AV/tool/provider reporters: automatic/review/retry,foreign/stale/revoked/
expired actors,worker/API privilege boundaries,atomic audio+cover approvals,
inaccessible quarantine,master preservation,expired attempts and crash recovery.
Synthetic MP3 frames test header mechanics only; they are NOT decoded audio or
real encoder acceptance. Actual selected ffmpeg/ffprobe/ClamAV plus signatures,
resource/RSS and B unit/011 installer/ACL/receipt/schema fingerprints remain open.
2GiB remains proposed and unqualified. No build feed/push/live migration/grant/
installation/security setting/real authority/UI change is performed by source.

The reproducible real-tool qualification entry is `python -m unittest
test_ingest_real_tools -v` with PYTHONPATH=.:tests. It runs ONLY when all six
explicit SHADIFY_TEST_SCANNER_ROOT/SCANNER_SHA256/MEDIA_ROOT/MEDIA_SHA256/
MEDIA_VERSION/DEFINITIONS fields name already staged read-only native closures
and real official signatures. It never installs/provisions them or reads a live
DB/profile: the test creates an isolated PG/root and synthetic owner/primary,
generates a one-second sine WAV and small PNG locally, requires the ordinary
no-match review and verifies actual scan/full decode/High+Low/master/cover/approval.
Its recorded metadata response remains a labelled offline fixture, so success
would prove AV/encode mechanics, not MusicBrainz acceptance for real user audio.
Without those fields it explicitly skips, and that skip is not acceptance.


Bounded review corrections: the private provider rate stamp is written to a
same-directory private temporary file, fully written/fsynced, atomically replaced
and directory-fsynced before any request. Interrupted writes preserve a complete
old or new reservation, never truncate the committed stamp. Crash-stage tests
verify restart spacing and no request before durability. Invalid historical rate
state still fails closed; no automatic reset bypasses the rate limit.

Every pipeline stage's final attempt/job transition now checks database lease
freshness and the claimed job state/revision again after evidence/approval work.
Expiration rolls back all approval/variant/derived rows and processing evidence.
A disposable-PG regression pauses AFTER the initial promotion fence until the
immutable lease expires, verifies the whole transaction rolled back, then proves
the next attempt reuses the private manifest/output bytes without re-encoding.
These corrections do not change011 SQL, roles, config, or activation decisions.


### Public CVD trust dependency (2026-10-06)

The production sandbox admits the exact public certificate leaf
`etc/clamav/certs/clamav.crt` in the existing sealed native runtime, pinned to the
official1.5.4 package SHA256
`60200e4b4b1b1b7257bb4550487aeb142034341f3300a8526e77a3134a940e9d`.
It is included in the existing scanner_runtime_sha256 inventory. No worker
configuration fields, SQL001..011 or role grants change. Other certificate files
and the leaf in a media closure are rejected; no generic certificate-store access.

The sealed definitions directory contains exactly six root-owned regular
single-link files: main.cvd, daily.cvd, bytecode.cvd, and each corresponding
`<family>-<version>.cvd.sign`. Decimal versions come from each complete512-byte
CVD header; no everchanging version is hardcoded. All three families are required.
Each sidecar is at most64KiB. Directories/ancestors and files must be root-owned,
non-symlink and not group/world writable. CLD is explicitly rejected until a
separate update/signature contract is qualified. No broad `*.sign` admission.

The certificate remains read-only in the existing root runtime mount; CVDs and
sidecars are individual read-only binds in /definitions, remounted read-only.
No new network, credentials, app-source mounts, descriptors or media-sandbox
access is added. Runtime inventory covers the certificate; scan evidence covers
CVD and public signing content hashes plus pre/post inode/size/mtime/ctime checks.
The public signing fingerprint is SHA256 of compact UTF-8 JSON of lexically
sorted `[basename, byte_count, file_sha256_hex]` arrays. Both CVD and public trust
fingerprints enter official-sha256 evidence within the original parent deadline.
Native libclamav still verifies the PKCS7 sidecars against exact CVD bytes and
the pinned certificate. Filename/hash/framing checks alone are not crypto proof.

The agreed Node collector contract copies the exact official CA leaf into its
sealed closure, derives sidecar names from CVD headers, seals files root:root0444,
and revalidates complete inventories on exit and saved-success reentry. Current
collectors rejecting `*.sign` need a separately owned forward fix and matched
reviewed tools input; never silently modify accepted owner440 material.

The approved WSL evidence snapshot is main63/daily28145/bytecode339. Versions are
test observations, not deployment pins. Wrong/missing/corrupt/mixed/changed public
material fails closed. Negative namespace tests retain private-file, network,
lock-FD and parent-death boundaries. Actual production NativeSandbox generated
WAV/PNG/harmless AV marker and native bad-signature evidence is recorded separately;
it is not FFmpeg, aggregate B-unit or full production pipeline qualification.
No installation, live definition update, persistent worker or runtime activation.
