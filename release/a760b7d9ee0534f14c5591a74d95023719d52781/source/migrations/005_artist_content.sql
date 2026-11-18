-- Review/apply explicitly after 004; no processing, entitlement or cloud provisioning.
BEGIN;
ALTER TABLE shadify_media_assets ADD CONSTRAINT media_artist_pair UNIQUE (id,artist_id);
ALTER TABLE shadify_media_assets DROP CONSTRAINT shadify_media_assets_asset_type_check;
ALTER TABLE shadify_media_assets ADD CHECK (asset_type IN
 ('audio_original','artwork_original','audio_delivery','audio_preview','artwork_delivery','video_delivery'));
ALTER TABLE shadify_media_assets DROP CONSTRAINT shadify_media_assets_access_class_check;
ALTER TABLE shadify_media_assets ADD CHECK (access_class IN ('private','free','paid'));
ALTER TABLE shadify_media_assets DROP CONSTRAINT shadify_media_assets_publish_state_check;
ALTER TABLE shadify_media_assets ADD CHECK (publish_state IN ('draft','published'));
ALTER TABLE shadify_media_assets ADD CHECK (asset_type NOT IN ('audio_original','artwork_original')
 OR (access_class='private' AND publish_state='draft'));
ALTER TABLE shadify_artists ADD FOREIGN KEY (avatar_asset_id,id) REFERENCES shadify_media_assets(id,artist_id);
ALTER TABLE shadify_artists ADD FOREIGN KEY (header_asset_id,id) REFERENCES shadify_media_assets(id,artist_id);
ALTER TABLE shadify_tracks
 ADD COLUMN data jsonb NOT NULL DEFAULT '{}',
 ADD COLUMN publish_state text NOT NULL DEFAULT 'draft' CHECK (publish_state IN ('draft','published')),
 ADD COLUMN access_class text NOT NULL DEFAULT 'free' CHECK (access_class IN ('free','paid')),
 ADD COLUMN original_asset_id uuid,
 ADD COLUMN delivery_asset_id uuid,
 ADD COLUMN preview_asset_id uuid,
 ADD COLUMN artwork_asset_id uuid,
 ADD FOREIGN KEY (original_asset_id,artist_id) REFERENCES shadify_media_assets(id,artist_id),
 ADD FOREIGN KEY (delivery_asset_id,artist_id) REFERENCES shadify_media_assets(id,artist_id),
 ADD FOREIGN KEY (preview_asset_id,artist_id) REFERENCES shadify_media_assets(id,artist_id),
 ADD FOREIGN KEY (artwork_asset_id,artist_id) REFERENCES shadify_media_assets(id,artist_id);
CREATE TABLE shadify_releases (
 id uuid PRIMARY KEY, artist_id uuid NOT NULL REFERENCES shadify_artists(id),
 data jsonb NOT NULL, cover_asset_id uuid,
 publish_state text NOT NULL DEFAULT 'draft' CHECK (publish_state IN ('draft','published')),
 UNIQUE (id,artist_id), FOREIGN KEY (cover_asset_id,artist_id) REFERENCES shadify_media_assets(id,artist_id)
);
CREATE TABLE shadify_release_tracks (
 release_id uuid NOT NULL, artist_id uuid NOT NULL, track_id uuid NOT NULL,
 position integer NOT NULL CHECK (position>=0), PRIMARY KEY (release_id,track_id),
 UNIQUE (release_id,position),
 FOREIGN KEY (release_id,artist_id) REFERENCES shadify_releases(id,artist_id) ON DELETE RESTRICT,
 FOREIGN KEY (track_id,artist_id) REFERENCES shadify_tracks(id,artist_id) ON DELETE RESTRICT
);
CREATE TABLE shadify_videos (
 id uuid PRIMARY KEY, artist_id uuid NOT NULL REFERENCES shadify_artists(id), data jsonb NOT NULL,
 track_id uuid, delivery_asset_id uuid,
 publish_state text NOT NULL DEFAULT 'draft' CHECK (publish_state IN ('draft','published')),
 FOREIGN KEY (track_id,artist_id) REFERENCES shadify_tracks(id,artist_id),
 FOREIGN KEY (delivery_asset_id,artist_id) REFERENCES shadify_media_assets(id,artist_id)
);
CREATE TABLE shadify_moments (
 id uuid PRIMARY KEY, artist_id uuid NOT NULL REFERENCES shadify_artists(id), data jsonb NOT NULL,
 author_account_id text NOT NULL, track_id uuid, image_asset_id uuid,
 publish_state text NOT NULL DEFAULT 'draft' CHECK (publish_state IN ('draft','published')),
 FOREIGN KEY (track_id,artist_id) REFERENCES shadify_tracks(id,artist_id),
 FOREIGN KEY (image_asset_id,artist_id) REFERENCES shadify_media_assets(id,artist_id)
);
CREATE TABLE shadify_events (
 id uuid PRIMARY KEY, artist_id uuid NOT NULL REFERENCES shadify_artists(id), data jsonb NOT NULL,
 publish_state text NOT NULL DEFAULT 'published' CHECK (publish_state='published')
);
COMMIT;
