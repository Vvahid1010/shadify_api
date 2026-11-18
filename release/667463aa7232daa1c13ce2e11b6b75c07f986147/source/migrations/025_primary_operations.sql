-- Source-only. No primary selection, role changes or backfill.
BEGIN;
CREATE TABLE shadify_primary_operations (
 operation_id uuid PRIMARY KEY,
 request_sha256 varchar(64) NOT NULL,
 authority_sha256 varchar(64) NOT NULL,
 authority_kind varchar(16) NOT NULL CHECK(authority_kind IN ('node','dev_operator')),
 result jsonb NOT NULL
);
COMMIT;
