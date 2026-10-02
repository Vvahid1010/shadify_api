# Initial authenticated API and private R2 uploads

The API composes existing canonical shell admission, PostgreSQL metadata and the
R2 S3 adapter. Native code/package readiness is separate from live Authentication,
database/schema, R2 or Cloudflare delivery proof. No service, database, migration,
cloud resource or credential was activated by this implementation.

## Current private API

All business routes use the existing Authentication peer through canonical
`app-security-shell==0.4.6`. All responses use Cache-Control: no-store.

| Method/path | Behavior |
| --- | --- |
| GET /api/me | Project a stable Shadify user from the admitted Authentication account; return existing owned artist IDs. |
| POST /api/artists/{artist_id}/tracks | Create a server-generated draft track under an already-owned artist; accepts only title. |
| GET /api/artists/{artist_id}/tracks | List that owner's draft tracks. |
| POST /api/media/uploads | Authorize direct private original upload after ownership/type/size checks. |
| POST /api/media/uploads/{asset_id}/complete | Verify object metadata/container signature and mark uploaded, private, draft. |
| GET /api/media/{asset_id} | Return owned asset metadata/state without storage keys or download URLs. |

Artist eligibility is not specified in the approved product docs. The API therefore
does not create an artist, infer artist capability from Authentication roles, or
grant ownership from request data. An approved existing artist/owner relationship
is required. No release publishing, payment, entitlement, analytics or worker
framework is added. There is no independent Shadify login/session system.

`migrations/001_media_foundation.sql` and `002_user_profile_track_drafts.sql` define
four small PostgreSQL tables: users, artists, tracks and media assets. User IDs,
track IDs and asset IDs are server-generated. The existing artists.owner_id remains
the canonical Authentication account ID. No binary media enters PostgreSQL.
Apply both migrations explicitly in order only to the assigned Shadify database;
they are never run during startup. No migration was run here.

## Authentication boundary

`GuardedASGI` and `AppSecurityBinding` are imported from the pinned canonical
package, not reimplemented. The receiver catalog admits only the Authentication
peer for these exact methods/routes and declares its existing generated headers.
The canonical shell verifies the entire logical request, including identity headers
and assertion, with native policy, authenticated transport and replay admission.

`auth.py` consumes identity only from the canonical admitted ASGI scope. It checks
one assertion/header occurrence, purpose, Shadify domain/audience, matching account/
session/auth-level fields and timestamps. It does not implement or pretend to
provide a separate standalone assertion-HMAC verifier. A raw browser assertion,
identity header, loopback address or config flag never establishes identity.
The source-default `main:app` has no identity override and remains closed.

The accepted identity boundary is canonical shell admission; no separate HMAC
verifier is required. Authentication/backend source remains unchanged here.

Native transport, root-selected connection/checkpoint, peer identity, keys and
healthy replay/clock bounds remain Node Agent-owned. The existing canonical ON
mode needs an assigned Redis replay allocation; this creates no job queue or new
service. Missing admission/transport/replay fails closed. Do not substitute a
process-local replay cache or caller header for those prerequisites.

## Upload behavior

Each authorization binds a fresh UUID original key, MIME, exact Content-Length,
configured short expiry and If-None-Match: *. Originals cannot be replaced under
the same key through a valid conditional PUT. Replacement requests get new IDs.
The browser sends the returned headers with a Blob body; it supplies Content-Length.
Keep signed URLs out of application/gateway/browser telemetry and logs.

Current supported originals: WAV/FLAC audio, JPEG/PNG artwork. Single-PUT limit
defaults to 100 MiB and can be lowered in Shadify's storage settings. Large video
multipart/resume remains deferred; it must be added before accepting large video.
No upload body passes through the application process or durable local disk.

Completion rechecks owner, track relationship and session expiry; HEAD plus a
bounded GET reads at most 4096 bytes for the container signature. Wrong size,
MIME or magic marks failed. Upload metadata insertion also checks current ownership
atomically in SQL. Uploaded never means processed, published or publicly playable.
Magic checks are basic container identification, not full decoding/safety proof.
Originals remain private/draft; no public or signed playback URLs are emitted.

## Native runtime and protected configuration

Node Agent catalog ID: `shadify-api`; package/app identity: `shadify_api`.
Runtime: Python 3.14, Linux x86_64, canonical native wheel with compatible glibc.
ASGI target: `shadify_api.runtime:app`. Runtime owner supplies
`CREDENTIALS_DIRECTORY` pointing to this instance's protected projected directory.
No deployment path, database host/name/user, unit, port or hostname is guessed by
the app. Node B is the chosen VM target; WSL remains development.

The directory contains owner-only regular non-symlink files (mode 0600), owned by
the assigned service identity or root if legitimately readable by that identity:

| Filename | Contents/owner |
| --- | --- |
| shadify_api.config.json | Shadify config_overlays.json shape plus approved canonical public app_security/app_security_binding projection. |
| app_profile.json | Existing native profile: schema_version 1, app shadify_api; generated/provisioned by runtime owner. |
| database.credentials.json | Explicit Shadify PostgreSQL assignment below; installer-owned. |
| storage.credentials.json | User-supplied R2 S3 credentials and Shadify upload settings below. |
| replay.credentials.json | Assigned protected Redis URL for canonical ON replay: {"url":"<assigned-redis-or-rediss-url>"}. |
| app_security.credentials.json | Canonical key projection, imported natively by the shell; never parsed/logged by app code. |

The exact deployed absolute directory/protected UI route is pending the Node Agent
owner's assignment. Put credentials only there once confirmed; never in chat,
Git, browser bundles, .env or another application's directory. The old WSL path
proposal is not a Node B credential destination or authorization to create it.

Database file (replace placeholders privately with the approved assignment):

```json
{"host":"<assigned-host>","port":5432,"dbname":"<assigned-Shadify-database>",
 "user":"<assigned-Shadify-role>","password":"<protected-password>",
 "sslmode":"verify-full","sslrootcert":"<approved-CA-file-if-needed>"}
```

Remote PostgreSQL requires TLS; sslmode disable is supported only for explicit
loopback assignments. No Mizfood identity/database defaults are present.

Storage file:

```json
{"provider":"r2","endpoint_url":"https://<32-character-account-id>.r2.cloudflarestorage.com",
 "bucket":"<existing-private-originals-development-bucket>",
 "access_key_id":"<R2-S3-access-key-id>","secret_access_key":"<R2-S3-secret-access-key>",
 "upload_ttl_seconds":300,"max_upload_bytes":104857600}
```

Use only existing bucket-scoped Object Read/Write S3 credentials. The API never
creates a bucket, credential, public grant or lifecycle rule. R2 endpoint/bucket/
credentials and upload policy remain Shadify-owned, not hardcoded in Node Agent.
Runtime loads no ambient AWS credentials. Invalid config/provider errors are
redacted; no real protected values were read, generated, printed or transmitted.

Missing DB/storage can keep process liveness available while readiness/business
operations stay pending. Invalid supplied protected config is rejected. Configured
startup performs no migration, listener binding, DB connection or R2 request.
GET /health is process liveness. GET /health/ready performs only a read-only schema
check and reports canonical binding state plus storage configured_unverified or
missing. Overall readiness stays 503 if DB/schema, binding or R2 config is missing;
configured_unverified is expressly not R2/network/bucket/CORS proof. No media worker
or payment dependency is invented as a readiness gate.

## Cloudflare and media policy

`shadify.org` is the selected main app hostname on Node B; `dev.shadify.org` stays
WSL. `media.shadify.org` is the accepted single media delivery hostname.
It is an R2 Custom Domain/public delivery boundary, not a tunnel hostname.
Use a separate public delivery bucket for derived/versioned audio/video/artwork/
previews; private originals/drafts stay in their separate private bucket.
Do not attach that private bucket wholesale to the public domain.

Direct uploads use the R2 S3 hostname, not the media domain. User configures CORS
for the exact approved frontend origin, PUT and returned Content-Type/If-None-Match
headers. Development and VM origins remain separately configurable; no wildcard
origin or bucket public grant is installed by this code. User owns media Custom
Domain, Cloudflare WAF/rate limits/cache controls and monitoring. No tunnel, DNS,
bucket or Cloudflare settings changed. Protected paid delivery remains deferred.

## Package and verification

`management_schema.json` declares the exact API catalog and protected runtime
filenames. `tools/native_release.py` packages committed source plus supplied offline
wheels in the existing native source/wheels/requirements.lock/build-info.json shape.
Manifest entries and resolved dependencies are hashed; credentials, venvs, tests,
media and mutable runtime data cannot enter the archive. Canonical shell wheel is
pinned by reviewed provenance, not fetched as an unverified public PyPI substitute.

`tools/publish_native_release.py` uses the existing release/<source_commit> and
latest.json three-version snapshot convention. It requires current published
main and uses an ordinary non-force push, retaining the prior build commit as
parent. No GitHub Actions test workflow or Node Agent install is added.

Local checks cover real SDK signing, stubbed SDK reads, no-listener lifecycle,
synthetic admitted-identity binding, ownership/state/metadata failures, protected
file validation and native artifact integrity. They establish code/package behavior,
not actual Authentication session, PostgreSQL migrations, R2 upload or edge delivery.

Next: finalize DB assignment and canonical identity boundary with runtime owner;
install the verified package/transport/profiles, explicitly apply the two migrations,
and map an approved owned artist. User privately supplies R2 settings in the
confirmed protected route. Only then run one tiny supported upload and prove CORS,
signature/type/length enforcement, overwrite rejection, expiry, ownership and
private/draft completion. Public-media/cache checks remain separate.


## Local Unix ingress and readiness correction

Node Agent's sealed adapter contract is local_unix_v1, not managed_nginx. The
API calls managed_uds.install_configured_receiver(binding, document, app_security,
clock_bounds=real_callback, storage_bounds=real_callback) and retains its returned
LocalUnixBinding. Canonical local_uds.ingress_protocol(channel, current=callback)
checks real SO_PEERCRED for na-authentication, exact channel commitments and
current selection before creating typed evidence. GuardedASGI clears any forged
admission marker and verifies the complete Authentication request; no independent
assertion HMAC verifier or copied transport/crypto is required.

Protected shadify_api.config.json includes exact app_security_transport shape:
{"profile":"local_unix_v1","role":"receiver","channels":{
"<connection-UUID>":{"node_id":"<B-UUID>",
"sender_app_instance_id":"<Authentication-instance-UUID>",
"recipient_app_instance_id":"<Shadify-instance-UUID>","generation":1,
"record_sha256":"<approved-local-record-sha256>","policy_revision":1,
"policy_digest":"<actual-native-context-transport-binding-digest>"}}}.
Node Agent supplies these exact values. They cannot be fabricated or selected
by a browser. The current callback compares the immutable process channel with
the actual native recipient status and connection revision/digest and becomes false on shutdown or mismatch.
Configuration changes require the owner-controlled process lifecycle.

Serve runtime:app with Uvicorn --http shadify_api.runtime:http_protocol on the
fixed /run/node-agent-shadify-api/receiver.sock. Ordinary H11 cannot create peer
evidence and business routes deny. Owner-selected directory0750/socket0660,
na-shadify-api owner/node_shadify_api_local group and na-authentication sender
are private grants distinct from the approved public frontend connector grant.
This API change provisions none of them.

The app follows the established local chronyc/ClockContinuity and Redis INFO
bounds checks: standalone master, no replicas/cluster/loading, noeviction,
positive memory bound and no AOF. Run-id/eviction epoch changes invalidate a
warmed store. The assigned protected replay.credentials.json supplies the URL;
no service, queue, background observer or Node Agent business gate is added.

Lifespan initializes canonical policy without DB/R2/Redis contact. Readiness
requires Active policy, current installed local adapter, healthy bounds,
successful canonical disposable replay reservation, PostgreSQL schema and
storage configuration. The readiness reservation touches only the app replay
namespace and expires normally (at most62seconds); no business dispatch occurs.
Canonical151-second recovery remains enforced and returns readiness503 while
recovering. Configured storage remains explicitly configured_unverified; this
is not real R2 or real end-to-end Authentication/VM acceptance.

tests/test_managed_ingress.py executes the exact local Unix protocol and native
ON shell through require_account to /api/me. SO_PEERCRED return and Redis command
backend are synthetic; crypto, policy, complete-message verification, recovery
and replay enforcement are canonical. It denies forged header/admission marker,
wrong UID/commitment/current selection, tampering, ON replay and bad clock bounds.
No listener, live Redis/DB/R2, operational credential, VM effect or grant occurs.

Package prerequisite: the previously approved transport0.1.8 wheel lacks local_uds.
Owner-reviewed transport source and immutable wheel/version/hash receipt are
required before corrected connected-release packaging/publication. Candidate
source tests alone do not establish that old wheel as deployable.


Candidate proof command (2026-10-02; owner source is under independent review):
`PYTHONPATH=/tmp/node-reporter-settings-main/backend .venv/bin/python -m unittest discover -s tests -q`
Result:41tests passed. node_agent_transport_dependency.json records exact public
candidate adapter file hashes separately from the previous0.1.8 wheel receipt.
Do not confuse those source hashes with a validated replacement wheel. The
builder rejects any transport wheel without local_uds and still requires the
exact assigned version/hash receipt; corrected build publication remains blocked.

The replay reader currently accepts protected password-bearing loopback redis://
or remote rediss:// URLs. A private Unix replay URL is not yet an assigned owner
contract and is not silently substituted. If the owner selects a Unix allocation,
coordinate its exact protected URL/ACL interface before connected activation.


Runtime Uvicorn is pinned0.53.0, within the existing transport wheel's declared
>=0.37,<0.54 support range. Offline candidate tests pass with that pin and
pip check finds no broken requirements. This does not supply or approve the
missing replacement transport wheel.
