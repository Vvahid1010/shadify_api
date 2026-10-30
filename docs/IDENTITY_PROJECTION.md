# Identity projection v1 — source preparation, 2026-10-29

Existing Auth worker delivers only proven associated `shadify` users to `shadify_api`.
Internal `POST /api/v1/identity-sync` requires canonical admitted Authentication peer, exact connection/revision,
site/scope, canonical body digest, version, purpose and <=60-second proof. No browser/user-assertion route.
The endpoint must be explicitly enrolled on both existing connection projections, including response `cache-control`.

Protected configuration field: `identity_site_scopes` = {"shadify": "<existing verified site UUID>"}.
Absent/empty remains disabled; null/unknown site/noncanonical UUID fails validation. Do not derive a pin from incoming claims.
Current Auth WSL has no pin for this site. Resolve/reuse an existing registered site before any new registration;
review login/factor effects before activation. Never assign or copy legacy factors automatically.

Migration: `migrations/023_identity_projection.sql`. Additive facts table only; no historical backfill or changes to existing local authority.
Received verified contacts become site-scoped exact-match digests and masked display values, never credentials.
Contact clearing/inactive/deleted states clear contact projections. Duplicate retries ACK deterministically,
older versions never overwrite newer facts; same-version different body conflicts. ACK identifies the received version.
No new public contact-search endpoint or permission policy is introduced by this extension.

Source tests are synthetic; source acceptance is not a live ACK. Activation separately requires current DB schema check,
private backup, explicit migration, exact existing-site scope pin and operation enrollment, compatible source selection
and one coordinated receiver restart. Auth API/worker configuration changes must use the same selected protected mapping.
No new service/queue, credential, factor, role, account relink, real message, order or broker action is included.
Rollback selects prior source and preserves additive tables; no destructive rollback after accepted projection writes.

Do not overwrite the installed WSL package with repository main: it has additional ProjectedReceiver/media composition. Apply only reviewed feature changes to a reconciled isolated snapshot. Background projection does not set first_admitted_at/admission_source or change active/profile/membership/admin.
