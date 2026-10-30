# Private primary control source preparation

Node contract: 7f4a5251ad78b4cad5f29819526d8dc9923f5cea. No public HTTP route,
listener, executable command or actual assignment is introduced. Historical
nomination routes and ordinary denied transfer remain unchanged.

`shadify_api.primary_control.PrimaryChange` binds operation_id, instance_id,
node_id, domain_app=shadify, factor_scope_id, target_user_id,
expected_primary_user_id (nullable), expected_revision and expires_at.
`apply_primary` and `observe_result` require independently verified admission;
default admission always rejects BEFORE DB access. An injected test reader is NOT
a real authority token. Its fingerprint/kind are audit provenance, not proof.
The reader must independently validate the exact protected node/installation/site/
scope/input/deadline and current operation on every invocation. Node owns its
same-operation TOTP and trusted helper/runner. Current shared Authentication-only
allowlist cannot authorize this API operation; no substitute keys or flags exist.

The caller must own a transaction and roll back every exception. Scope lock/CAS,
active canonically admitted user plus exact active identity projection, audit and
operation result commit atomically. Duplicate exact requests return their original
result; changed-content replay/stale revision reject. Old/new independent delegate
grants, profiles, projection versions and artist memberships stay untouched.
Result observation requires fresh protected read authority; unknown results are
not permission to create a new operation. Receipts are individually bounded and
not automatically pruned; no replay-breaking retention rule is introduced.

Migration025 creates only result storage. It requires the existing admin schema
and projection023; it must not be auto-applied merely because images024 are
installed. No live migration is authorized by this document. No destructive
rollback of retained operation receipts is provided.

The approved local-only DEV command uses the same domain transaction, but a
verified DEV launcher/operator/site binding is still required. Caller dev flags,
an arbitrary reference, OS UID alone or a user role do not establish that binding.
Production automatic enablement is forbidden. Public route isolation is currently
structural: nothing imports this module into routes/main/nomination transport.
User-model alignment remains a separate pending decision.

Source qualification, 2026-10-30: the existing disposable PostgreSQL fixture
applied all source migrations through025 and passed initial/replacement CAS,
concurrent contenders (one winner), concurrent exact replay, changed-input replay
rejection, final-admission rollback and lost-response result observation. Delegates,
user profile and projection versions were preserved. The fixture has no TCP/live
database fallback and cleaned up its temporary cluster. Independent source review
found no remaining publication blocker. This does not qualify real Node/DEV
admission or authorize any installed migration, assignment or service action.
