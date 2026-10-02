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
