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
All checks use synthetic files/identities or stubbed reads, not deployed secrets.
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
Health /health is liveness; /health/ready reports pending DB/binding/R2 configuration
without claiming real storage proof. Details/checklist: docs/API_FOUNDATION.md.


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
