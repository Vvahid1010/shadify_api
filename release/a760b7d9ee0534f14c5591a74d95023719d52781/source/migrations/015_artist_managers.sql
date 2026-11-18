-- Additive after the existing 014_artist_profile_media identity; no seeded grants.
-- Reviewed source preparation. Apply only in an explicitly reviewed deployment.
BEGIN;
CREATE TABLE artist_manager_grants (
 artist_id uuid NOT NULL REFERENCES shadify_artists(id) ON DELETE RESTRICT,
 user_id uuid NOT NULL REFERENCES shadify_users(id) ON DELETE RESTRICT,
 permissions text[] NOT NULL CHECK (cardinality(permissions)>0 AND
 permissions <@ ARRAY['edit_profile','manage_content','publish_content']::text[]),
 PRIMARY KEY (artist_id,user_id)
);
CREATE INDEX artist_manager_grants_user ON artist_manager_grants(user_id,artist_id);
COMMIT;
