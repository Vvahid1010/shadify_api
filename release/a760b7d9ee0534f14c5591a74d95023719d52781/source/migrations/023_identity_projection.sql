-- Explicit additive migration. No user backfill, provenance or authority changes.
BEGIN;
CREATE TABLE auth_identity_projections (
 user_id uuid NOT NULL REFERENCES shadify_users(id) ON DELETE RESTRICT,
 domain_app varchar(128) NOT NULL CHECK (domain_app='shadify'),
 factor_scope_id varchar(36) NOT NULL,
 version bigint NOT NULL CHECK(version>0), payload_sha256 varchar(64) NOT NULL,
 identity_status varchar(16) NOT NULL CHECK(identity_status IN ('active','pending','disabled','blocked','deleted')),
 email_digest varchar(64),phone_digest varchar(64),email_masked varchar(254),phone_masked varchar(32),
 PRIMARY KEY(user_id,domain_app,factor_scope_id)
);
CREATE INDEX ix_auth_identity_email_scope ON auth_identity_projections(domain_app,factor_scope_id,email_digest);
CREATE INDEX ix_auth_identity_phone_scope ON auth_identity_projections(domain_app,factor_scope_id,phone_digest);
COMMIT;
