-- Protected pending contact selectors only; no accounts, grants or backfill.
BEGIN;
CREATE TABLE shadify_primary_selections (
 operation_id uuid PRIMARY KEY REFERENCES shadify_primary_operations(operation_id),
 domain_app varchar(32) NOT NULL CHECK(domain_app='shadify'),
 selection_revision integer NOT NULL CHECK(selection_revision>0),
 node_id uuid NOT NULL, instance_id uuid NOT NULL, factor_scope_id varchar(36) NOT NULL,
 expected_primary_id uuid, expected_primary_revision bigint NOT NULL,
 email_digest varchar(64), phone_digest varchar(64),
 email_masked varchar(254), phone_masked varchar(32),
 state varchar(24) NOT NULL CHECK(state IN ('pending','ambiguous','assigned','superseded','authority_changed')),
 assigned_id uuid, assigned_revision bigint,
 UNIQUE(domain_app,selection_revision),
 CHECK(email_digest IS NOT NULL OR phone_digest IS NOT NULL)
);
COMMIT;
