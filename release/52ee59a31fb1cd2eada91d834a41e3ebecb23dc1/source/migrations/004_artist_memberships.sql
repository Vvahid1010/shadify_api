-- Apply after 003 and reviewed canonical provenance preparation. Abort ambiguous owners.
BEGIN;
DO $$ BEGIN
 IF EXISTS (SELECT 1 FROM shadify_artists a LEFT JOIN shadify_users u
 ON u.auth_account_id=a.owner_id WHERE u.id IS NULL OR NOT u.active
 OR u.first_admitted_at IS NULL OR u.admission_source IS DISTINCT FROM 'canonical_shell')
 THEN RAISE EXCEPTION 'Legacy artist owners lack active canonical admission provenance; cutover aborted';
 END IF;
END $$;
ALTER TABLE shadify_artists
 ADD COLUMN slug varchar(64),
 ADD COLUMN name varchar(120) NOT NULL DEFAULT '',
 ADD COLUMN bio text NOT NULL DEFAULT '',
 ADD COLUMN genres jsonb NOT NULL DEFAULT '[]',
 ADD COLUMN socials jsonb NOT NULL DEFAULT '[]',
 ADD COLUMN publish_state text NOT NULL DEFAULT 'draft' CHECK (publish_state IN ('draft','published','suspended')),
 ADD COLUMN verified boolean NOT NULL DEFAULT false,
 ADD COLUMN avatar_asset_id uuid,
 ADD COLUMN header_asset_id uuid,
 ADD COLUMN layout jsonb NOT NULL DEFAULT '{}';
UPDATE shadify_artists SET slug='artist-' || id::text;
ALTER TABLE shadify_artists ALTER COLUMN slug SET NOT NULL;
ALTER TABLE shadify_artists ADD CONSTRAINT artist_slug_unique UNIQUE (slug);
ALTER TABLE shadify_artists ADD CONSTRAINT artist_slug_shape CHECK
 (slug ~ '^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$');
CREATE TABLE artist_memberships (
 id uuid PRIMARY KEY, artist_id uuid NOT NULL REFERENCES shadify_artists(id) ON DELETE RESTRICT,
 user_id uuid NOT NULL REFERENCES shadify_users(id) ON DELETE RESTRICT,
 role text NOT NULL DEFAULT 'owner' CHECK (role='owner'),
 assigned_at timestamptz NOT NULL DEFAULT clock_timestamp(), revoked_at timestamptz,
 CHECK (revoked_at IS NULL OR revoked_at>=assigned_at)
);
CREATE UNIQUE INDEX artist_one_active_owner ON artist_memberships(artist_id) WHERE revoked_at IS NULL;
INSERT INTO artist_memberships(id,artist_id,user_id)
 SELECT a.id,a.id,u.id FROM shadify_artists a JOIN shadify_users u ON u.auth_account_id=a.owner_id;
CREATE TABLE shadify_admin_grants (
 user_id uuid PRIMARY KEY REFERENCES shadify_users(id) ON DELETE RESTRICT,
 granted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 granted_by text NOT NULL, revoked_at timestamptz,
 CHECK (length(granted_by)>0)
);
CREATE TABLE artist_ownership_audit (
 id uuid PRIMARY KEY, artist_id uuid NOT NULL REFERENCES shadify_artists(id) ON DELETE RESTRICT,
 actor_user_id uuid NOT NULL REFERENCES shadify_users(id) ON DELETE RESTRICT,
 actor_account_id text NOT NULL, action text NOT NULL CHECK (action IN ('assign','transfer','revoke')),
 old_owner_account_id text, new_owner_account_id text,
 occurred_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE FUNCTION protect_artist_ownership_audit() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'Ownership audit is append-only'; END $$;
CREATE TRIGGER artist_ownership_audit_immutable BEFORE UPDATE OR DELETE ON artist_ownership_audit
 FOR EACH ROW EXECUTE FUNCTION protect_artist_ownership_audit();
ALTER TABLE shadify_artists DROP COLUMN owner_id;
COMMIT;
