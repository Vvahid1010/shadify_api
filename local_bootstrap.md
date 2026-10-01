# Shadify API native WSL contract

Authoritative source: `/srv/Coding_space/shadify_api` on Ubuntu ext4.
Toolchain: native WSL Python >=3.12, FastAPI/Uvicorn, psycopg and boto3.
Runtime: source-only, inactive. There is no Shadify API managed venv, unit,
listener, database assignment or credential directory installed. Do not start
an ad hoc service or reuse another application's runtime/identity.

Shared runtime owner: `/srv/Coding_space/Topology_bootstrap-/local/wsl`.
Its manifest, systemd/nginx templates, protected configuration and installation
workflow own activation. Node Agent integration remains a later coordinated step.

## Coordinated activation proposal (not installed)

- Stable downstream/proxy app identity: `shadify_api` (distinct from Mizfood).
- Domain app: `shadify`, coordinated with the Authentication/frontend owner.
- Candidate loopback receiver: `127.0.0.1:24002`, observed unused 2026-10-01;
  recheck/reserve in shared manifest before use. Native app-security mTLS shell
  transport must be integrated before exposing this receiver through Authentication.
- Liveness: `GET /health`; readiness: `GET /health/ready`. Liveness is process
  evidence only. Readiness currently returns 503, including when dependencies
  are injected, because no live Authentication/PostgreSQL/R2 check exists yet.
- Logical ASGI app: `shadify_api.main:app`; default instance deliberately has
  no cloud/database wiring and rejects upload endpoints with 503.
- Browser upload-session routes should go through the existing same-origin
  Authentication proxy. Proposed downstream paths: `POST /api/media/uploads`
  and `POST /api/media/uploads/{asset_id}/complete`. Final ingress prefix and
  canonical app-security binding belong to the parent/shared topology owner.

Authentication's `domain_apps.shadify.proxy_app` needs a separately registered
`proxy_apps.shadify_api`, its canonical outbound destination/endpoint binding,
and a distinct Shadify app profile/assertion registry. Identity dependency must
use the pinned canonical shared verifier and validate purpose/audience, domain,
session/account, expiry and transport. Do not copy crypto or trust raw headers.
The existing Authentication service is unchanged by this foundation.

## Local verification without runtime activation

An isolated source-local `.venv` now exists for explicitly authorized dependency
and offline SDK validation. It is ignored by Git and is not a managed application
runtime. No other application's venv was changed. Python 3.14.4 and the exact
26 API/test dependency versions are recorded in `requirements.lock`.

From this source directory in Ubuntu:

```bash
.venv/bin/python -m pip check
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -v
```

For a fresh isolated environment, use the exact validated pins from trusted PyPI:

```bash
python3 -m venv .venv
.venv/bin/python -m pip --isolated install -r requirements.lock
.venv/bin/python -m pip --isolated install --index-url https://pypi.org/simple --no-deps -e .
```

Latest validation: 19 tests pass; pip check reports no broken requirements.
The real installed boto3/botocore signer validates PutObject parameters locally,
including signed content-length, content-type and if-none-match headers.
SDK HeadObject/GetObject models are tested with botocore Stubber, not R2.
Uvicorn imports the real ASGI target and completes startup/shutdown lifespan
without binding a listener; database connections and outbound HTTP/network are
blocked in that check. In-process ASGI liveness is 200, readiness/upload routes
remain 503 even with forged identity headers. Protected config validation uses
only synthetic unit-test files. Starlette emits a non-failing httpx TestClient
deprecation warning; the exact tested versions are pinned.

No managed environment, database migration, R2 request or runtime activation
occurred. Live Authentication/PostgreSQL/R2 and browser upload proof remain pending.

See `docs/API_FOUNDATION.md` for exact protected storage configuration and the
pending integration verification sequence.
