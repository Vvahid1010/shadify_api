-- Candidate only: explicit operator application; never applied at startup.
BEGIN;
ALTER TABLE shadify_releases ADD COLUMN revision bigint NOT NULL DEFAULT 0 CHECK (revision >= 0);
ALTER TABLE shadify_tracks ADD COLUMN independently_published boolean NOT NULL DEFAULT false;
-- Historical published tracks have no recoverable album-only provenance.
UPDATE shadify_tracks SET independently_published=true WHERE publish_state='published';
ALTER TABLE shadify_tracks ADD CONSTRAINT shadify_independent_publication
 CHECK (NOT independently_published OR publish_state='published');
COMMIT;
