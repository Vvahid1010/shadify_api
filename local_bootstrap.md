# Shadify API native development and package contract

Source: `/srv/Coding_space/shadify_api` on Ubuntu WSL ext4. Toolchain: native
Python 3.14. The ignored repo-local `.venv` is for isolated tests/build preparation.
No Shadify API managed WSL venv, unit, listener, DB or credentials are installed.
Do not start an ad hoc service. Shared WSL runtime remains owned by
`/srv/Coding_space/Topology_bootstrap-/local/wsl`.

Node B is the selected VM placement for Shadify app/Authentication/API. Node Agent
owns installation, lifecycle and manual/switchable automatic GitHub updates.
App hostname is shadify.org; dev.shadify.org remains WSL. Candidate WSL port 24002
is a historical unreserved proposal, not a VM or runtime assignment. No port,
database placement or cross-app credentials are silently reused.

## Repeatable local checks

```bash
.venv/bin/python -m pip check
env -u CREDENTIALS_DIRECTORY PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -v
```

Registry dependency versions are in requirements.lock. Canonical private native
shell 0.4.6 is supplied separately using the exact producer-approved wheel/provenance
in app_security_dependency.json; do not fetch a public same-name substitute.
Checks use synthetic identities/object storage, not deployed secrets. Artist
tests also start disposable native PostgreSQL 18 with a private temporary Unix
socket and no TCP listener; it is closed/deleted afterward. Native test binaries
must already be available; there is no external database fallback or installation.
The non-failing Starlette httpx TestClient deprecation warning is known.

## Native release

Catalog shadify-api; app/package shadify_api. Existing runtime format: Python 3.14,
Linux x86_64; source/ plus offline wheels/, hash requirements.lock, canonical
build-info.json. The release includes SQL migrations but never auto-applies them.
Build only from the exact clean committed source and a validated complete wheelhouse:

```bash
.venv/bin/python tools/native_release.py --source <full-main-commit> --wheels <prepared-wheelhouse> --output <new-archive-path>
.venv/bin/python tools/publish_native_release.py --source <published-main-commit> --archive <verified-archive-path>
```

Publisher uses the existing three-version build-branch snapshot layout and a normal
non-force push. It does not activate/update a VM or broaden repository access.
Node Agent installer compatibility and VM acceptance remain its owner's work.

Runtime ASGI target: shadify_api.runtime:app with CREDENTIALS_DIRECTORY
provided by the runtime owner. Protected filenames: shadify_api.config.json,
app_profile.json, database.credentials.json, storage.credentials.json,
replay.credentials.json and canonical app_security.credentials.json as applicable.
Absolute protected paths, DB, peer/transport/checkpoint and replay/clock bounds
remain unassigned prerequisites. No startup migration or cloud probe exists.
Health /health is liveness. /health/ready scope=profile_database checks canonical
admission and schema006 for page/content metadata management; optional storage is
missing or configured_unverified, not real R2 proof. It does not establish playback,
guest ingress, processing, video/profile uploads, payments or followers. Self-profile
editing has no endpoint. Details/checklist: docs/ARTIST_PROFILES_AND_ACCESS.md.


Managed receiver installation prerequisites are specified in
docs/API_FOUNDATION.md (Local Unix ingress and readiness correction): immutable
app_security_transport in the existing protected config; standard
local_uds.ingress_protocol(channel, current=callback) via runtime:http_protocol on the fixed Unix listener; assigned protected
replay.credentials.json; working chronyc and bounded standalone replay Redis.
Node Agent owns that listener/projection/installation. Main policy Active alone
does not confer readiness. No changes to shared WSL or live VM were made.
For offline combined native proof, install the test extras into the isolated
venv (cryptography 50.0.2 and pqcrypto 1.0.0) and run unittest discovery. These
fixture generators are not included in the production offline wheel package.


The reviewed transport is now pinned0.1.9 with a full public build receipt in
node_agent_transport_dependency.json. The offline package carries the exact
reviewed wheel and its hashed lock. API source tests now use that installed wheel
directly; no owner source-path overlay is required. Live configuration and Unix
socket/replay ownership grants remain Node Agent's separate activation scope.


Phase 1 media contract: docs/R2_SIGNED_MEDIA_DELIVERY.md. storage.credentials.json
optionally sets playback_url_ttl_seconds (default 900) independently of upload TTL.
The current reviewed4ec0b0d package remains the deployment baseline; signed GET
provider/policy preparation adds no HTTP playback route, schema/publication or
Worker and implies no live bucket/DNS/credential effect.

The optional playback TTL field is accepted by the updated source reader, not
the current4ec0b0d baseline. Keep that deployed projection unchanged until an
updated compatible package is explicitly selected. Source proof:51offline tests
pass and pip check is clean; actual HTTP/playback/publication/R2 proof is pending.


Current app-owned admin source targets006_app_admin, additive after005_artist_content. Contract and exact
schema/enrollment/app-owned bootstrap/provenance prerequisites:
docs/ARTIST_PROFILES_AND_ACCESS.md. Old 002 packages/schema cannot be mixed with
this cutover. Local testing/packaging does not authorize migration, publication,
grant or shared topology changes. Source-ASGI public metadata is tested; guest
managed ingress and playback HTTP remain inactive.

## Local media source contract (2026-10-04; not live activation)

Runtime API storage is optional. To select local media later, the Node owner
supplies owner-only protected `storage.credentials.json` with:

```json
{"provider":"local","local_root":"<assigned absolute private directory outside source/build>","media_base_url":"https://<approved app host>","upload_ttl_seconds":300,"playback_url_ttl_seconds":900,"max_upload_bytes":104857600}
```

Protected `media-signing.credentials.json` contains exactly `{"key":"<base64
32-byte media signing key>"}`. No real key/root is created here. Both processes
receive the assigned CREDENTIALS_DIRECTORY; optional STORAGE_PROVIDER must match
the protected provider. Local config accepts no R2 credential fields.
LocalAdapter requires runtime/root ownership, 0700-style root permissions,
no symlink components and Linux user xattrs. Owner chooses durable filesystem,
capacity/backup/quota and service identity. WSL and B are separate allocations.

Existing API startup/managed receiver remains unchanged. Node ownership must
separately launch native Uvicorn with factory
`shadify_api.media_runtime:app_factory`, factory mode, `--no-access-log`, on an
owner-assigned private socket/listener. Route only `/media/{UUID}` PUT/GET/HEAD
from the approved HTTPS app origin to it; preserve Range/Content-Type/Length/
If-None-Match/optional x-amz-checksum-sha256. Never route `/api` or broadly bypass
GuardedASGI and never relay upload bytes through Authentication. No port/socket/
service/proxy setting is chosen or installed by this source task. Listener has
no documentation, directory, issuance, admin or business routes. Same-origin
delivery needs no permissive CORS. Guest issuance remains explicitly pending.

R2 future config keeps existing account-scoped endpoint/bucket/S3 credentials,
adds the same media_base_url and optional existing local_root for mixed object
providers, and uses the same separate media signing file. New-upload default
switching is distinct from object migration. See MEDIA_STORAGE_ARCHITECTURE.md
for copy+full-checksum verification, per-object cutover and rollback.

No Node B API installation, live filesystem, key, database migration, R2 call,
cloud resource or publishing is established by the isolated tests.

The byte ASGI handler is source-implemented, but the Node owner interface request
currently prohibits a new Node service/listener. Existing managed native HTTP
rejects unsigned bytes before ASGI. Therefore the factory above is a concrete
handler contract, not an approved operational unit/socket instruction. Resolve
the narrow transport with Node ownership before any projection/installation;
never infer approval for a second service or weaken canonical admission.

### Node-agreed media ingress correction

The prior new-listener restriction is resolved by the coordinated Node owner:
one small Shadify media unit, same API UID, dedicated Unix socket
`/run/node-agent-shadify-media/receiver.sock`, and only assigned database/storage/
media-signing custody; it has no canonical transport/replay/signing keys. Node
owns exact service/socket/root modes and source construction. nginx may route
only `/media/{UUID}` PUT/GET/HEAD with exactly one token query and GET
`/media/v1/tracks/{UUID}/access` with only optional preview=true|false. No retries,
no full body buffering, no access-query logs, existing bounded body/timeout
controls; no /api bypass or upload through Authentication. The GET access route
performs real DB free/preview checks, never invents a guest Authentication identity.
This resolves the source contract, not live installation or transport proof.
