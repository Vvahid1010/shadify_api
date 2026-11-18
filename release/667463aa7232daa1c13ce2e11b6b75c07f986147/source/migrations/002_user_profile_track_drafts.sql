-- Apply after 001, only to the explicitly assigned Shadify database. Never auto-run at startup.
BEGIN;
CREATE TABLE shadify_users (
    id uuid PRIMARY KEY,
    auth_account_id text NOT NULL UNIQUE
);
ALTER TABLE shadify_tracks ADD COLUMN title varchar(200) NOT NULL DEFAULT '';
COMMIT;
