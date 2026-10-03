-- Additive preparation; explicit operator application only, never startup.
BEGIN;
ALTER TABLE shadify_users
 ADD COLUMN active boolean NOT NULL DEFAULT true,
 ADD COLUMN first_admitted_at timestamptz,
 ADD COLUMN last_admitted_at timestamptz,
 ADD COLUMN admission_source text CHECK (admission_source='canonical_shell'),
 ADD COLUMN name varchar(120) NOT NULL DEFAULT '',
 ADD COLUMN slug varchar(64) UNIQUE,
 ADD COLUMN bio text NOT NULL DEFAULT '',
 ADD COLUMN genres jsonb NOT NULL DEFAULT '[]';
ALTER TABLE shadify_users ADD CONSTRAINT identity_provenance_complete CHECK
 ((first_admitted_at IS NULL AND last_admitted_at IS NULL AND admission_source IS NULL)
 OR (first_admitted_at IS NOT NULL AND last_admitted_at IS NOT NULL AND admission_source='canonical_shell'));
COMMIT;
