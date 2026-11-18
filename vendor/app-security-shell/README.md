# app-security-shell 0.4.6

Read the [shared adoption contract, revision 2](../docs/APP_SECURITY_SHELL.md)
before integrating this package into an application. Package version and adoption
contract revision are independent.

Reusable in-process Rust protocol/policy/key core, CPython 3.12–3.14 binding, and
shared HTTP/ASGI adapters. Wallet Manager owns this canonical directory.
Protocol v1 uses AWS-LC ML-KEM-768, HKDF-SHA256, AES-256-GCM, Ed25519 and
ML-DSA-65. Both signatures are mandatory; responses commit to the signed request.
There is no Python crypto fallback, identity generator, sidecar or public API.

## Local build and test

The approved Wallet development paths are `.venv/` (Python), `.build-tools/`
(Rust, caches, disposable tests) and `dist/security-shell/` (artifacts).
`tools/run` selects these paths and removes `CREDENTIALS_DIRECTORY`. It does not
start either app. Do not point tests at operational credentials or services.
Rust 1.90.0 is pinned in `rust-toolchain.toml`; `Cargo.lock` locks native crates.
Python hash locks are separate for wheel construction and isolated validation.

Run from the Wallet repository root, with the approved tools already installed:

```sh
security_shell/tools/run uv pip install --python .venv/bin/python --require-hashes -r security_shell/requirements-test.lock
security_shell/tools/run cargo fmt --manifest-path security_shell/Cargo.toml --all -- --check
security_shell/tools/run cargo test --manifest-path security_shell/Cargo.toml -p app-security-core --locked
security_shell/tools/run cargo check --manifest-path security_shell/Cargo.toml -p app-security-python --locked
security_shell/tools/run cargo clippy --manifest-path security_shell/Cargo.toml --workspace --all-targets --locked -- -D warnings
security_shell/tools/run maturin build --release --locked --manifest-path security_shell/python-native/Cargo.toml --interpreter .venv/bin/python --out dist/security-shell
security_shell/tools/run uv pip install --python .venv/bin/python --no-deps dist/security-shell/app_security_shell-0.4.2-cp312-cp312-*.whl
security_shell/tools/run python -m pytest security_shell/tests -q
```

Redis tests create only their own temporary process, TCP disabled, under
`.build-tools/test-runs/r-*`, with a private Unix socket. They stop that exact
process and remove its socket/directory. Multiprocess/thread tests require a
runner permitting normal thread/event-loop shutdown; the desktop sandbox on
this host stalls even a trivial `asyncio.to_thread` program. The approved
unsandboxed isolated runner was used; this is not application activation.

Docker build inputs were removed by the native-delivery migration on main.
Compiled components must build in native GitHub Actions; managed nodes consume
verified output without a Rust toolchain. Shell CI publication and Node Agent
admission are pending; local wheel tests do not establish managed delivery. The local
wheel's platform tag is specific to its build host; do not assume compatibility
with Node Agent's future managed runtime. Build on the target baseline and check
wheel tags before installation there.

## Future consumer and artifact contract

The same **versioned dependency** is installed into each app's own environment;
apps do not share a mutable Python virtual environment. Local artifacts live in
`dist/security-shell/`; this is an agreed development artifact location, not an
already configured production Node Agent repository.

`tools/export.py` exports only Git-committed canonical package files into a new
empty destination and writes `PROVENANCE.json` (full Wallet commit, deterministic
source digest, package/protocol versions). It rejects symlinks, private-key files,
credential files, binaries and build output. The `check` action verifies exact
content, mode, inventory and provenance against that commit. Do not manually edit
an exported snapshot. Signatory phase two will consume one at
`vendor/app-security-shell/`; this phase does not write the Signatory repository.

```sh
security_shell/tools/run python security_shell/tools/export.py export --commit <full-wallet-commit> --destination <new-snapshot-directory>
security_shell/tools/run python security_shell/tools/export.py check --commit <same-full-commit> --destination <snapshot-directory>
security_shell/tools/run python security_shell/tools/artifact.py --wheel <wheel-path> --output <artifact-manifest-path>
```

An artifact manifest records SHA256, source commit/digest, locks and runtime tags.
It is integrity/provenance metadata, not a signature or a production install ACK.
The producer must commit the package first; dirty canonical sources are rejected.
Audit commands used here: `cargo-audit==0.22.0` and `pip-audit==2.9.0`. Existing
Python application dependency findings are recorded in the result document;
do not interpret a successful wheel build as an advisory-free application.

## Integration contract

Applications supply an exact endpoint catalog and approved public connection
projection. Rust rejects malformed JSON, duplicate/unknown fields, unknown peer
or route, policy mismatch, wrong generation/key IDs and noncanonical base64.
The catalog admits literal ASCII paths and deterministic whole-segment `{name}`
templates. Ambiguous templates, `%`, traversal, backslashes, duplicate separators,
regexes and streaming/multipart remain rejected. Literal v1 policy bytes are unchanged.

`ContextRegistry.prepare(directory, config_bytes, catalog_bytes)` imports the
fixed `app_security.credentials.json` natively; `install(candidate)` explicitly
activates a validated snapshot. No private credential JSON is handed to Python.
The file's SHA256 must equal the public snapshot binding. Every directory is
opened without following symlinks; final directory/file owner and permissions,
regular file, link count and same-descriptor stability are checked. Service
identity/credential projection belongs to the future control plane.

`Client` and `SyncClient` use a registry and the approved connection-bound base
URL. ON POSTs `/_app-security/v1/message` under that base prefix; only a verified
response becomes an ordinary business response. OFF uses ordinary logical HTTP
but validates the exact policy and request. Redirects, content encoding,
unknown outcomes and invalid responses never trigger a fallback or retry.
Header order/duplicates are preserved for declared headers. Host/content-length,
hop framing and generated user-agent/accept-encoding are transport metadata,
not protected logical headers. Accept, content-type and app routing/correlation
headers are catalog-declared. Never trust a caller header as sender identity.

`Middleware` wraps the existing business app, intercepts only declared receiver
paths and the carrier, reserves replay before dispatch and reconstructs the
request once. Unrelated app routes keep their existing behavior. ON and OFF require a
server-supplied `TrustedPeer`; absence denies before body/replay/dispatch. The actual mTLS ingress mapping is
not implemented or proven by this package. Responses are nonstreaming and capped
at 1 MiB body / 2 MiB outer wire. Request freshness is 60s, future allowance 30s;
a response gets a fresh issuance time after completion. Explicit handler budget
(default 30s, up to 3600s) must match the receiver's supported business contract.

`RedisReplayStore` requires two independent trusted health predicates:
`clock_bounds` and `storage_bounds`; storage defaults to false. Deployment must
establish bounded clock drift plus no eviction, no independent reservation loss,
and invalidation on cache failover/restart/clock discontinuity. Continuity-marker
recovery waits 151 monotonic seconds. Atomic Lua reservation is never released
on business failure, cancellation or lost response. Tests of these gates do not
establish those deployment guarantees. There is no in-memory runtime fallback.

New work reacquires/rechecks the registry after waits, so revoke/replacement
cannot admit a queued old context. Already admitted request tickets own immutable
contexts until response completion. Registry rollback floors/revocations are
process-local; restart rollback protection requires a trusted future Node Agent
projection. `drain()` blocks new admissions; then close `NativeJobs` and drain
application requests. Native work is bounded to four running plus sixteen queued
by default, shared by Wallet callers; cancelled queued work keeps its capacity
until dequeued and skips execution. Python still owns HTTP/plaintext buffers;
zeroization claims apply only to shell-owned native buffers and provider-owned
secret lifetimes, not complete process-memory erasure or OS isolation.

## Redis dependency ownership — 0.1.1

The consuming application creates, configures and pins its Redis client, then
injects it into `RedisReplayStore`. The shell does not import redis-py or declare
a runtime Redis dependency. Wallet Manager retains redis 6.4.0 in its application
requirements and the existing shell test lock; Signatory retains redis 7.4.1.
Replay storage/clock/continuity gates and atomic Lua behavior are unchanged.
Package 0.1.1 changes dependency metadata only; it does not change the native
protocol, crypto, glibc target or either application's installed environment.
The existing 0.1.0 wheel retains its old Redis requirement; consume the new
identified wheel after its normal admission, never relabel/overwrite the old one.

Validation: the complete shell suite passed 56 tests with redis-py 6.4.0 and
56 with 7.4.1 using disposable private Redis fixtures and temporary import
overlays. Existing installed environments were unchanged. See
`../docs/APP_SECURITY_SHELL_RESULT.md` for wheel hash and evidence scope.

## ON/OFF adoption — 0.1.2

Real Wallet outbound endpoints support both modes; default remains ON. Policy
is validated before deriving key requirements: OFF-only/empty enrollment skips
credential reads; any ON endpoint requires local material and keys for its
connection. Optional peer keys preserve the original ON serialization when present.
An empty enrollment still needs valid supplied identity metadata to install a
context; a placeholder baseline is not a fabricated runtime identity.

OFF requires `trusted_egress` in both clients and `TrustedPeer` from the receiver's
trusted ingress. The egress adapter attests the exact client/channel, destination,
identities and policy, not just that the URL says HTTPS. Its actual Node Agent
implementation is a runtime integration gate. Missing evidence denies before I/O.
Request/response policy metadata is checked against the immutable native snapshot;
metadata alone is not peer identity. See the shared contract for the exact header,
conditional key schema and app composition interface. Version 0.1.2 is required at
both OFF consumers; there is no compatibility fallback to unverified plain replies.

The supported managed target floor is glibc 2.38. Check wheel architecture/ABI and
actual required libc before installation. A local development build with uncommitted
source is not an immutable release receipt; export/record provenance only after
canonical source is committed through a separately authorized publication step.

## Public app-neutral composition (0.2.0)

Use `binding.AppSecurityBinding` with one public snapshot provider (native
`app_security` + schema-1 `app_security_binding`), protected directory provider,
static local receiver catalog, and optional app-owned async HTTP provider/replay
factory. Remote catalogs and exact destinations come from the approved projection.
`receiver.GuardedASGI` protects declared routes and the carrier before native
initialization; unrelated health/lifespan do not initialize crypto or replay.
`request` / `request_sync` take an explicit connection ID, supported method,
concrete path, raw query bytes, ordered headers and bounded body bytes.
`post_json` / `post_json_sync` are serialization conveniences over those methods.
The owned snapshot is rechecked before I/O, and no request can choose its mode.

`install_integration` accepts trusted ingress/egress and independent replay clock/
storage predicates (false by default). These are code hooks, never config-loaded
callbacks. Replay factories return `(store, async_close)` and allocate lazily for
ON only. `prepare/install/status/revoke/drain/close` wrap the existing lifecycle;
close waits for admitted work before native/replay/owned-HTTP shutdown. Construct
one binding per consuming process, never reuse a parent process binding after fork.
See the revision-2 shared contract for bounds, ownership and unsupported formats.

## Python 3.14 validation

The operator-approved `.build-tools/python314/` environment uses existing WSL
CPython 3.14 without changing system Python or the 3.12 `.venv`. Install portable
test inputs with `uv pip install --python .build-tools/python314/bin/python
--require-hashes -r security_shell/requirements-common.lock`. Build with the
explicit `--interpreter .build-tools/python314/bin/python`; install only the
matching cp314 wheel there. Python metadata compatibility does not establish
ABI compatibility: cp312 and cp314 wheels remain separate artifacts.
This platform change adds no SSE/header capabilities or live adoption.

## Explicit proxy capabilities (0.4.0)

Endpoint records may declare strict boolean `response_stream` and `proxy_headers`.
Both default false and serialize only when true; enabled capabilities affect the
policy digest and require a newer coordinated connection revision. Old endpoint
digests are unchanged. Stream declarations are GET-only and require explicit OFF
at both peers; ON is denied, never retried or downgraded. The native validator
permits only declared request X-Forwarded-For/Host/Proto and response Set-Cookie
when proxy_headers is true. Cookie, reserved and framing headers remain forbidden.
Forwarding values never establish peer identity.

`AppSecurityBinding.route_metadata` returns public endpoint metadata and the base
from one installed snapshot. `open_stream(connection_id, path, deadline=<absolute
monotonic>, ...)` admits an OFF stream using the same destinations/identities as
bounded requests. It returns status_code, ordered headers, aiter_bytes(), run()
and aclose(). Always close, even on abandoned iteration. Wrap downstream ASGI
response delivery in `stream.run(response(...))` so lifetime includes final send.
No synchronous stream API exists. Bounded APIs reject stream declarations.

Sender and receiver have separate caps of 16 streams per owner. Deadline maximum
is 3600 seconds; receiver handler/request limits can be tighter. Silent opening,
reading and blocked downstream delivery are interrupted on timeout/revoke/drain
with a 50ms policy polling bound; cleanup is bounded to two seconds. A stream uses
native jobs only for admission/head validation. Identity and one immutable policy
snapshot are retained; no reconnect or retry occurs. Non-SSE responses are bounded
to 1 MiB before exposure. Gzip is optional, single-member, decoded in <=64KiB
outputs with an expansion ceiling max(1MiB,100*compressed bytes); truncated/trailing
or unsupported encoding rejects. A raw transport chunk exceeding 1MiB rejects.
Actual SSE chunks are delivered before EOF with natural downstream backpressure.
TransportFailure.reason is `timeout` for typed bounded/opening timeout errors;
outcome still distinguishes not_sent from outcome_unknown. Cleanup cannot create
retry authority. No Node Agent trust adapter or deployment is supplied here.

## Mandatory channel admission (0.4.1)

Envelope signatures authenticate the message; they never replace the separately
required managed transport. Async and sync finite sends require `TrustedEgress`
for both ON and OFF before I/O. Missing or mismatched client/destination/identity/
connection/revision/digest evidence is `not_sent`. The selected snapshot is
rechecked after the callback and immediately before sending. No retry/fallback.

Both receiver modes require `TrustedPeer` before reading the body. ON additionally
calls native `InboundRequest.admit_transport(connection_id, node_id, app_instance_id)`
to compare the channel peer with authenticated envelope metadata before reserving
replay or dispatching the handler. Wrong/missing evidence denies; native key/crypto,
wire v1, response correlation and conservative replay/lifetime contracts are
unchanged. Native method exposes no private keys or metadata and does not consume
the response ticket. OFF continues through its native plain-policy admission.

This intentionally tightens the 0.4.0 adapter behavior: applications with keys but
no installed trusted channel hook now deny ON as well as OFF. Real hook delivery
still belongs to Node Agent. Test helpers are synthetic and must never become
runtime attestations. Existing 0.4.0 and earlier wheels do not contain this check.


## Admission after replay awaits (0.4.2)

Replay checks both trusted health predicates before and after the awaited atomic
Redis reservation. False, non-boolean and failing observations invalidate local
continuity and deny dispatch. A reservation created before the denial remains
present; no retry/release path is introduced. The ASGI receiver also rechecks
request expiry immediately before calling the business application after replay
admission. Policy/config/wire version 1 and native cryptography are unchanged.

This closes the demonstrated 3.S post-EVAL health race. Health callbacks still
need a trusted deployment adapter, persistent clock-step detection and the full
151-second continuity-loss quarantine. It does not install Node Agent callbacks,
reconcile managed app identity/assertion fences or establish a managed Nginx
channel. Source/wheel tests do not establish VM runtime readiness.

### Finite response limits (0.4.3)

Endpoint catalogs may declare `max_response_bytes` for finite GET responses,
from 1 MiB (the unchanged default) through 8 MiB. Explicit default and omission
normalize identically; a larger value enters the connection policy digest and
requires coordinated catalog/revision updates by both peers. POST and streams
cannot acquire this override. Requests retain 1 MiB bodies and 2 MiB carrier
bounds. Response carrier/decoded framing has an endpoint-bound budget of twice
the response body limit, including native pending-request admission. ON and OFF
use the same native policy; no mode fallback or identity relaxation is added.
Older packages reject non-default declarations rather than ignoring them.

An admitted handler exceeding its finite response limit returns a bounded,
authenticated 502 with `response_too_large`, numeric `response_bytes` and
`limit_bytes`; the original body and headers are discarded. This is response
failure after handler execution, not proof that a mutation was not executed.
No automatic retry is introduced. Application-specific pagination should keep
normal chart payloads well below the defensive limit.

## Managed transport binding metadata — package 0.4.5

`AppSecurityBinding.route_metadata(..., include_transport_binding=True)` adds
the native sender/recipient identity and policy tuple to the selected route
metadata. The route and tuple come from the same installed native snapshot;
the default result remains unchanged. The tuple contains no private key and
grants no channel trust by itself. A managed transport adapter must still
establish the local socket and remote mTLS peer before creating trusted egress.

## SSE lifetime separation — package 0.4.4

Only native-admitted `response_stream` OFF endpoints use the stream budgets.
`GuardedASGI` and `Middleware` keep finite `request_timeout`/`handler_timeout`
unchanged. Once admitted, the receiver replaces the finite outer budget with
`stream_timeout` (default 3600 seconds); initial response headers and bounded
non-SSE errors still have the handler opening budget. `stream_idle_timeout`
(default 60 seconds) bounds silent streams and blocked downstream delivery.
A successfully delivered nonempty body chunk refreshes idle only. Neither
heartbeats nor reconnect preparation renew the stream maximum or native authority.

`AppSecurityBinding.open_stream` accepts separate `open_timeout` (default 30)
and `idle_timeout` (default 60). Setup, admission, pool wait and response headers
share the opening deadline; it is never restarted after setup. Socket reads use
the idle limit. The caller's absolute `deadline` continues to cap all delivery
and must be bounded by its real assertion/session authority. Disconnect, expiry,
revocation, drain and backpressure retain the existing single-owner cleanup.
Header values and untrusted scope flags cannot select this exception.

This release also retains the previously exported 0.4.3 per-endpoint finite
response allowance (Wallet export eba4baa718d0f138145be80df767ec88835e69b0),
which was deployed to the chart consumers before its merge into canonical main.
It preserves the newer canonical rotation support; no catalog or crypto downgrade
is part of the SSE correction.


## SSE disconnect cleanup — package 0.4.5

An AnyIO/Starlette disconnect can repeatedly cancel an in-progress HTTP/1 close
after httpcore marks its body closed but before the connection becomes CLOSED.
The request counter then reaches zero while an ACTIVE socket retains a pool slot.
The canonical guarded owner shields and drains the same cancelled children before
returning; repeated raw asyncio cancellation is retained and re-raised after drain.
This preserves the existing timeout/authority limits without enlarging the pool.
AnyIO is now an explicit dependency; isolated checks cover 4.11 and 4.15 consumers.
The two-second close timeout bounds the upstream response close attempt. As before,
owned handler/native work must cooperate with cancellation: unfinished children
are not abandoned or falsely reported as released to meet a wall-clock deadline.

## Unified 0.4.6 source

This release combines selected native transport binding metadata with the
AnyIO cancellation drain for SSE. The route and transport tuple come from the
same native snapshot; cancellation still drains owned HTTP reads before pool
release. No protocol version or endpoint admission rule changes.

## Protected systemd credentials — 0.4.6

The native reader also accepts systemd LoadCredential files below
`/run/credentials/<unit>`: root ownership, a read-only RAM mount and the exact
ACL granting read access only to that service UID. App-owned private files keep
the previous contract. Snapshot hashes, identity/key validation, symlink/hardlink
checks, protocol and request authorization are unchanged. No copy or startup
controller is introduced. Actual systemd composition must be tested separately
from ordinary file fixtures.
