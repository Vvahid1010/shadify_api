-- Review/apply only to the assigned Shadify PostgreSQL database during coordinated activation.
BEGIN;
CREATE TABLE shadify_artists (
    id uuid PRIMARY KEY,
    owner_id text NOT NULL -- Existing Authentication account_id, not a new login.
);
CREATE TABLE shadify_tracks (
    id uuid PRIMARY KEY,
    artist_id uuid NOT NULL REFERENCES shadify_artists(id),
    UNIQUE (id, artist_id)
);
CREATE TABLE shadify_media_assets (
    id uuid PRIMARY KEY,
    owner_id text NOT NULL,
    artist_id uuid NOT NULL REFERENCES shadify_artists(id),
    track_id uuid NOT NULL,
    key text NOT NULL UNIQUE,
    asset_type text NOT NULL CHECK (asset_type IN ('audio_original','artwork_original')),
    mime text NOT NULL,
    size bigint NOT NULL CHECK (size > 0),
    expires_at timestamptz NOT NULL,
    storage_provider text NOT NULL DEFAULT 'r2',
    state text NOT NULL CHECK (state IN ('upload_pending','uploaded','processing','ready','failed')),
    access_class text NOT NULL DEFAULT 'private' CHECK (access_class='private'),
    publish_state text NOT NULL DEFAULT 'draft' CHECK (publish_state='draft'),
    FOREIGN KEY (track_id,artist_id) REFERENCES shadify_tracks(id,artist_id)
);
COMMIT;
