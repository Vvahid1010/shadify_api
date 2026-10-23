-- Additive app-owned authority. Historical shadify_admin_grants stays inert.
-- No identity/primary/delegate is imported or selected by this migration.
BEGIN;
CREATE TABLE shadify_app_admin (
 singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
 primary_user_id uuid REFERENCES shadify_users(id) ON DELETE RESTRICT,
 revision bigint NOT NULL DEFAULT 0 CHECK (revision >= 0)
);
INSERT INTO shadify_app_admin(singleton) VALUES (true);
CREATE FUNCTION protect_shadify_primary() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'Primary state cannot be deleted'; END IF;
 IF OLD.primary_user_id IS NOT NULL AND NEW.primary_user_id IS NULL
 THEN RAISE EXCEPTION 'Primary requires a successor'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER shadify_primary_successor BEFORE UPDATE OR DELETE ON shadify_app_admin
 FOR EACH ROW EXECUTE FUNCTION protect_shadify_primary();
CREATE TABLE shadify_app_admin_delegates (
 user_id uuid PRIMARY KEY REFERENCES shadify_users(id) ON DELETE RESTRICT,
 permissions text[] NOT NULL CHECK (cardinality(permissions) > 0 AND
 permissions <@ ARRAY['create_artist','edit_profile','manage_content','publish_content',
 'change_owner','suspend','verify','edit_user_profile']::text[])
);
CREATE TABLE shadify_app_admin_audit (
 id uuid PRIMARY KEY,
 actor_user_id uuid REFERENCES shadify_users(id) ON DELETE RESTRICT,
 actor_account_id text,
 target_account_id text,
 artist_id uuid REFERENCES shadify_artists(id) ON DELETE RESTRICT,
 action text NOT NULL,
 details jsonb NOT NULL DEFAULT '{}',
 occurred_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TRIGGER shadify_app_admin_audit_immutable BEFORE UPDATE OR DELETE
 ON shadify_app_admin_audit FOR EACH ROW EXECUTE FUNCTION protect_artist_ownership_audit();
COMMIT;
