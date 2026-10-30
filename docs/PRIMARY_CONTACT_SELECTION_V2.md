# Private primary contact selection v2

Source-only, aligned with Node contract at
`6806de6e219ced3b0228ed3fb0db58d31d8b0aa9` and operator TXT SHA256
`6b58e0e24a93c20b150be159bd597acd820f3a82665389a4c6a8af33e07d6e08`.

## Executable boundary

App-owned module `shadify_api.primary_selection` provides `read_state`, `select_contact`,
`observe_selection` inside caller-owned psycopg connection transactions. Every exception must
roll back the entire transaction. There is NO public HTTP endpoint, installed CLI,
new listener, or executable Node/DEV dispatcher. Default admission returns503
before any DB access. Existing UUID-only private transaction remains compatible.

Node must supply an independently verified admission reader bound to exact installed
app/source/contract, database/instance/node/site/factor-scope, purpose and complete
normalized typed request. It must recheck after waits and before commit. The
reader's audit fingerprint is NOT proof. A test callback, UID, environment variable,
manifest flag, earlier login or `approved`/`totp_verified` does not enable execution.
Node alone checks fresh same-operation panel TOTP; API never receives code or key.
The API repository cannot finish the missing protected Node runner/verifier or DEV
launcher; those remain separately owned and unavailable.

## Typed input

`ContactSelection`: `contract="primary-contact-v2"`, UUID `operation_id`,
`node_id`, `instance_id`, `factor_scope_id`, exact `domain_app`, nullable `email`
and `mobile` (at least one), nullable UUID `expected_primary_id`, nonnegative
`expected_primary_revision`, `expected_selection_revision`, integer `expires_at`.
Extra fields are forbidden. Email uses existing trim/lower normalization; mobile
must be full `+[0-9]{8,15}`, with no country guess or partial match.
`SelectionRead` has only contract/operation/node/instance/site/scope/deadline fields.
All calls require independent scoped authority, including read/result calls.

`read_state` returns state, primary_id, primary_revision, selection_revision and
nullable masked selection. `select_contact`/`observe_selection` return:

```json
{"receipt":{"state":"accepted","operation_id":"UUID","selection_revision":1},
 "selection":{"operation_id":"UUID","state":"pending","selection_revision":1,
 "current":true,"email_masked":"a***@example.test","mobile_masked":null,
 "assigned_id":null,"assigned_revision":null},
 "primary_id":null,"primary_revision":0}
```

Receipt acceptance is immutable and is NOT an assignment receipt. Selection state
is freshly read: pending, ambiguous, assigned, superseded or authority_changed.
`current` means latest selection generation, not an assertion that its historical
assigned UUID is still current primary; compare top-level primary_id/revision.
Unknown operation returns `state=unknown`; absent selection is distinct from an
unavailable schema/authority. Same operation with changed normalized request409;
same-content retry observes current result, never repeats assignment. A result read
uses fresh authority even if original admission deadline has expired.

## Durable matching and privacy

Only existing verified projection digests in exact site/factor scope are matched,
email OR mobile, one distinct eligible account. Shadify also requires genuine prior
canonical admission; projection cannot fabricate it. No match remains pending;
two accounts remain ambiguous. Old primary persists until unique resolution.
No user creation/invite, verification, backfill, forced sync or role/profile changes.

Pending data stores scoped digests plus existing masked display, never raw contacts,
proof or TOTP. Digests reuse lookup semantics and are not encryption; protect the
database under existing app storage rules and never log requests/selectors. Request
commitment covers normalized contacts and all scope/CAS/deadline fields.

The scope lock precedes identity writes and matching. Selection and primary CAS,
receipt, assignment, existing audit and consumption share one transaction. Replacing
a pending selection supersedes its generation atomically. Any intervening authority
revision change (including delegated-admin changes) conservatively produces
authority_changed, requiring explicit new selection; no automatic reapproval.
An accepted pending selection has no invented short TTL. Only admission has a
deadline; trusted later identity arrival consumes the persisted admission once,
without replaying expired credentials. Later contact reuse never moves its UUID.
Concurrent arrivals cannot predict future conflicts; after completed assignment a
later conflicting contact cannot transfer authority. Existing inactivity checks remain.

## Integration and rollout

Reconciliation is wired into existing verified projection transactions and genuine
first-admission path; Auth payload/version/ACK/retry semantics remain unchanged.
No polling daemon/service. Required additive source migration `026_primary_selections.sql` follows
`025_primary_operations.sql`. No migration or assignment was executed against operational databases.
Deploy code only with the complete required schema; preserve installed package
composition and obtain runtime approval separately. Rollback code can retain added
tables; do not discard accepted selections/audit as a downgrade shortcut.

Pending source publication does not make Node assignment operational. Node must
implement verified protected dispatch before exposing the form/TOTP action.


Deferred reconciliation additionally requires current trusted recipient node/instance
and registered factor scope, from the existing verified receiver/first-admission
composition, matching the stored selection. Missing or different installation is
non-resolving; no incoming user/contact field can supply that binding. Shadify wires
its protected ProjectionReceiver; Mizfood legacy receiver returns its verified local
recipient. Mizfood's managed projection adapter currently lacks an executable
verify_projection method in the reviewed baseline; it cannot claim deferred managed
readiness until its owner supplies the equivalent verified binding. No fallback or
fake binding was added. Immediate selection also remains unavailable until real
Node admission verifies the same installation/database binding.
Runtime schema/permissions must include reading selection rows for identity hooks;
only the protected selection writer may insert accepted operations. No operational
DB grants have been changed. Identity hooks without recipient evidence never assign.

## Source qualification — 2026-10-30

31 focused selection/projection/revision tests passed with disposable unprivileged PostgreSQL, including late-policy-revocation rollback. Wider admin suite: 20 passes plus 7 subtests; five failures independently reproduced unchanged on published baseline a240dae (three artist-language fixture cases and two missing-admin-schema cases).
Independent review closed lock ordering, current recipient binding and commit-time verification findings. No kernel/Node operational authority test or live assignment was performed.
