# Initial same-node Shadify API profile/database package contract

Historical profile candidate qualification,2026-10-03. Current artist candidate
status and prerequisites: [ARTIST_PROFILES_AND_ACCESS.md](ARTIST_PROFILES_AND_ACCESS.md). Node Agent's sole writer owns
all allocator/lifecycle/local-pair/DB/grant/VM effects. This repo provisions none.
Do not publish or select the candidate before bounded review. Current deployment
baseline remains source4ec0b0d/buildc04ef5c until coordinated selection.

Candidate is main's signed-media preparation plus the minimal R2-optional
profile/database readiness correction. Exact source/archive/manifest digests are
returned in the qualification receipt; no parent-approved security code is forked.
The receiver retains reviewed transport0.1.9 (wheel SHA256
0a7566754891f473b025c3781646599225b251cf6d6b6b15e0adc4c956e853cf),
canonical shell0.4.6 and Uvicorn0.53.0. Auth's mixed-sender0.1.10 need not change
the receiver's package if it preserves the exact local_unix_v1 wire contract.

## Native package and launcher

Existing format only: Python3.14/Linuxx86_64/glibc-compatible canonical wheel,
source/,29 offline wheels, hash requirements.lock, canonical build-info.json.
Catalog shadify-api/app identity shadify_api. ASGI runtime:app and Uvicorn
--http shadify_api.runtime:http_protocol, fixed private Unix listener
/run/node-agent-shadify-api/receiver.sock. No TCP/public API listener is introduced.
Local receiver protocol requires kernel sender UID na-authentication and exact
connection/generation/record/policy-revision/policy-digest headers. The proposed
na-shadify-api/node_shadify_api_local ownership and directory0750/socket0660
remain the installer's selected private grant, not this task's effect.

## Protected projected inputs

Runtime owner supplies CREDENTIALS_DIRECTORY. Required owner-only regular files:

| File | Exact consumed shape |
| --- | --- |
| shadify_api.config.json | app_id=shadify_api (default); database_file=database.credentials.json and storage_file=storage.credentials.json (defaults); canonical app_security/app_security_binding plus app_security_transport below. Unknown top-level fields deny. |
| app_profile.json | schema_version=1, app=shadify_api; installer-owned existing profile. |
| app_security.credentials.json | Canonical validated key/snapshot projection; not app-parsed. |
| database.credentials.json | host,dbname,user,password required; port defaults5432; sslmode defaultsverify-full, optional sslrootcert. Remote sslmode=disable denies. No ambient libpq/Mizfood defaults. |
| replay.credentials.json | Exact {"url":"<assigned-protected-Redis-URL>"}; current reader accepts password-bearing loopback redis:// or remote rediss://. Unix replay URL remains an unassigned contract; do not silently substitute one. |

storage.credentials.json is OPTIONAL and should be omitted for this initial
profile/database slice. No R2 credential/resource is required or read. If supplied,
the storage file is validated; invalid values deny startup rather than disappear.
Updated source accepts optional playback_url_ttl_seconds with default900; baseline
4ec0b0d does not, so never add it to a baseline projection before the matching
reader package is approved/selected. No deployed directory/path/secret value is
invented or inspected here.

Exact public local receiver projection:

```json
{"profile":"local_unix_v1","role":"receiver","channels":{
"<connection-UUID>":{"node_id":"<B-UUID>",
"sender_app_instance_id":"<Authentication-instance-UUID>",
"recipient_app_instance_id":"<Shadify-instance-UUID>","generation":1,
"record_sha256":"<approved-local-record-sha256>","policy_revision":1,
"policy_digest":"<actual-canonical-policy-digest>"}}}
```

Policy must contain exactly that Authentication->shadify_api connection on B,
distinct installed app instances and its approved endpoint IDs. No fake defaults,
direct native enrollment call, fabricated policy digest or new assertion HMAC.
First profile endpoint: shadify_api.me.read, GET /api/me. Canonical headers include
x-auth-assertion/account/session/auth-level with domain_app=shadify and
proxy_app=shadify_api; catalog declares all existing generated forwarding headers.
Auth/Node Agent own admission, mixed sender routing and exact domain projection.

## App schema and health

Current source migration target is005_artist_content; prior profile candidate
target was002. Apply nothing operationally from this task. Coordinated owner
selection must review 003 provenance preparation, 004 guarded owner backfill and
005 content tables with the matching package. Missing old owner proof aborts 004;
002-only schema keeps candidate readiness closed. App startup never migrates.
ready() performs read-only SELECT LIMIT0 across required artist/content/authority
columns. Exact schema/activation prerequisites have one source in the artist contract. This app DB target is not
Node Agent's own schema version or another app's database.
Any approved live schema005/package cutover must first have a validated database
backup and reviewed restore plan covering schema/data and post-backup writes.
004 removes artists.owner_id, so old application code alone is not a rollback.
No live migration/backup/restore or grant is authorized by this documentation.

GET /health is process liveness200; no listeners/cloud/DB probes at startup.
GET /health/ready scope=profile_database requires Active canonical policy/current
local receiver, healthy real clock/Redis bounds, canonical replay reservation
outside151-second recovery and schema compatibility. R2 may remain missing while
that scope is ready200. The scope is page/content metadata management, not
operational playback, guest ingress, processing, video/profile uploads, payments
or followers. No self-profile edit endpoint exists. Missing schema, wrong peer, lost continuity or recovery
keeps readiness503. Media upload endpoints remain503 without storage even after
successful native admission. Storage configured_unverified is never real R2 proof.
Profile GET projects a stable Shadify user for the admitted Auth account and
returns existing artist_ids; no artist/admin membership or entitlement is created by profile projection.

## Source-only proof and remaining owner work

Focused tests exercise actual installed native ingress/shell/identity/runtime
composition with synthetic kernel peer/Redis/database fixtures. They prove core
readiness200 without any storage file/R2 client, admitted /api/me, admitted media
denial503 and readiness503 when schema fails. Existing wrong-peer/forgery/tamper/
ON replay and readiness-first epoch recovery regressions remain required.
This is not real DB/session/VM acceptance. Owner supplies exact DB/replay/clock,
private socket/grant, canonical pair/domain/config and component lifecycle; bounded
review precedes candidate package publication or installation. No business
publication/paid-entitlement rule or R2 signed-media decision is changed here.
