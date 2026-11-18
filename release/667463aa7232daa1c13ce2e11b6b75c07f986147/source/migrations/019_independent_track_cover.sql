-- Independent private track-cover processing. No lookup, grants or history rewrite.
BEGIN;
DO $preflight$ BEGIN
 IF (SELECT encode(sha256(convert_to(prosrc,'UTF8')),'hex') FROM pg_proc
     WHERE oid='validate_shadify_ingest_decision()'::regprocedure)
    NOT IN ('8ba616d83481489a9a5f0d00781e23317f3dc333f5bc98a80fdd673999cac6fa',
                '55d683835bc7ec420deebf8e8175f15961980e7db9b1fa4a8b39b02681801191')
 THEN RAISE EXCEPTION 'Decision validator differs from reviewed installed018 baseline'; END IF;
 IF (SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE
     conrelid='shadify_ingest_match_decisions'::regclass AND conname='shadify_match_kind_rules')
    IS DISTINCT FROM $expected$CHECK ((((kind = 'audio'::text) AND (cover_audio_decision_id IS NULL) AND (((method = 'automatic'::text) AND (actor_user_id IS NULL) AND (selected_recording_mbid IS NOT NULL)) OR ((method = 'reviewed'::text) AND (actor_user_id IS NOT NULL)) OR ((method = 'not_requested'::text) AND (actor_user_id IS NULL) AND (selected_recording_mbid IS NULL) AND (corrected_metadata = '{}'::jsonb)))) OR ((kind = 'cover'::text) AND (method = 'automatic'::text) AND (actor_user_id IS NULL) AND (selected_recording_mbid IS NULL) AND (cover_audio_decision_id IS NOT NULL)) OR ((kind = 'profile'::text) AND (method = 'automatic'::text) AND (actor_user_id IS NULL) AND (selected_recording_mbid IS NULL) AND (cover_audio_decision_id IS NULL))))$expected$
 THEN RAISE EXCEPTION 'Cover decision constraint differs from reviewed018'; END IF;
 -- Existing immediate FK proves the artwork's actual same-artist track.
 -- The restricted worker needs no SELECT or UPDATE privilege on tracks.
 IF NOT EXISTS(SELECT 1 FROM pg_constraint WHERE conrelid='shadify_media_assets'::regclass
  AND confrelid='shadify_tracks'::regclass AND contype='f' AND convalidated AND NOT condeferrable
  AND pg_get_constraintdef(oid)='FOREIGN KEY (track_id, artist_id) REFERENCES shadify_tracks(id, artist_id)')
 THEN RAISE EXCEPTION 'Validated immediate media same-track FK required'; END IF;
END $preflight$;
ALTER TABLE shadify_ingest_match_decisions DROP CONSTRAINT shadify_match_kind_rules;
ALTER TABLE shadify_ingest_match_decisions ADD CONSTRAINT shadify_match_kind_rules CHECK(
 (kind='audio' AND cover_audio_decision_id IS NULL AND
  ((method='automatic' AND actor_user_id IS NULL AND selected_recording_mbid IS NOT NULL) OR
   (method='reviewed' AND actor_user_id IS NOT NULL) OR
   (method='not_requested' AND actor_user_id IS NULL AND selected_recording_mbid IS NULL AND corrected_metadata='{}'::jsonb))) OR
 (kind='cover' AND actor_user_id IS NULL AND selected_recording_mbid IS NULL AND
  ((method='automatic' AND cover_audio_decision_id IS NOT NULL) OR
   (method='not_requested' AND cover_audio_decision_id IS NULL AND corrected_metadata='{}'::jsonb))) OR
 (kind='profile' AND method='automatic' AND actor_user_id IS NULL AND selected_recording_mbid IS NULL AND cover_audio_decision_id IS NULL));
DO $adapt$ DECLARE definition text; BEGIN
 SELECT pg_get_functiondef('validate_shadify_ingest_decision()'::regprocedure) INTO definition;
 IF (length(definition)-length(replace(definition,$anchor$ IF NEW.method='not_requested' THEN$anchor$,'')))/length($anchor$ IF NEW.method='not_requested' THEN$anchor$)<>1
 THEN RAISE EXCEPTION 'Exactly one reviewed optional decision branch required'; END IF;
 EXECUTE replace(definition,$anchor$ IF NEW.method='not_requested' THEN$anchor$,$branch$ IF NEW.kind='cover' AND NEW.method='not_requested' THEN
  IF source_asset.asset_type IS DISTINCT FROM 'artwork_original'
   OR source_asset.access_class IS DISTINCT FROM 'private' OR source_asset.publish_state IS DISTINCT FROM 'draft'
   OR source_asset.artist_id IS NULL OR source_asset.track_id IS NULL
   OR NEW.actor_user_id IS NOT NULL OR NEW.selected_recording_mbid IS NOT NULL OR NEW.cover_audio_decision_id IS NOT NULL
   OR NEW.corrected_metadata IS DISTINCT FROM '{}'::jsonb
   OR evidence IS DISTINCT FROM '{"complete":true,"provider":"independent_artwork","policy":"track-cover-independent-v1","metadata_lookup":"not_requested"}'::jsonb
   OR NOT EXISTS(SELECT 1 FROM shadify_ingest_stages v JOIN shadify_ingest_stages e
    ON e.job_id=v.job_id AND e.input_sha256=v.input_sha256 AND e.scan_id=v.scan_id
    JOIN shadify_ingest_stages m ON m.id=NEW.matching_stage_id AND m.job_id=v.job_id AND m.input_sha256=v.input_sha256 AND m.scan_id=v.scan_id
    JOIN shadify_ingest_scans s ON s.id=v.scan_id AND s.job_id=v.job_id AND s.input_sha256=v.input_sha256
    WHERE v.job_id=NEW.job_id AND v.input_sha256=NEW.input_sha256
     AND v.stage='validation' AND v.payload @> '{"passed":true}'::jsonb
     AND e.stage='extraction' AND e.payload @> '{"complete":true}'::jsonb
     AND s.verdict='clean' AND s.full_coverage=true)
   THEN RAISE EXCEPTION 'Validated independent track cover evidence required'; END IF;
  RETURN NEW;
 END IF;
 IF NEW.method='not_requested' THEN$branch$);
END $adapt$;
COMMIT;
