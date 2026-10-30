-- Optional metadata for new private audio; source proposal only.
-- Preserve exact installed 014 or staged 016 validator, including video guards.
BEGIN;
DO $preflight$ DECLARE definition text; BEGIN
 SELECT prosrc INTO definition FROM pg_proc WHERE oid='validate_shadify_ingest_decision()'::regprocedure;
 IF encode(sha256(convert_to(definition,'UTF8')),'hex') NOT IN ('7530a604932e01b5f5bacd0643e50f1a3c4f676f8ab32d5bf5bc9774f8d67378', 'c19d553ac2ded1b8f3a9cd8f34b230bcb108dd838c1b41806eb119cf9831b718')
 THEN RAISE EXCEPTION 'Decision validator differs from reviewed 014/016 baseline'; END IF;
 IF (SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid='shadify_ingest_approvals'::regclass AND conname='shadify_ingest_approvals_match_decision_check') IS DISTINCT FROM $constraint$CHECK (((jsonb_typeof(match_decision) = 'object'::text) AND (match_decision ? 'decision'::text) AND (jsonb_typeof((match_decision -> 'decision'::text)) = 'string'::text) AND ((match_decision ->> 'decision'::text) = ANY (ARRAY['automatic'::text, 'reviewed'::text]))))$constraint$ THEN RAISE EXCEPTION 'Existing policy constraint changed: shadify_ingest_approvals_match_decision_check'; END IF;
 IF (SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid='shadify_ingest_match_decisions'::regclass AND conname='shadify_ingest_match_decisions_method_check') IS DISTINCT FROM $constraint$CHECK ((method = ANY (ARRAY['automatic'::text, 'reviewed'::text])))$constraint$ THEN RAISE EXCEPTION 'Existing policy constraint changed: shadify_ingest_match_decisions_method_check'; END IF;
 IF (SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid='shadify_ingest_match_decisions'::regclass AND conname='shadify_match_kind_rules') IS DISTINCT FROM $constraint$CHECK ((((kind = 'audio'::text) AND (cover_audio_decision_id IS NULL) AND (((method = 'automatic'::text) AND (actor_user_id IS NULL) AND (selected_recording_mbid IS NOT NULL)) OR ((method = 'reviewed'::text) AND (actor_user_id IS NOT NULL)))) OR ((kind = 'cover'::text) AND (method = 'automatic'::text) AND (actor_user_id IS NULL) AND (selected_recording_mbid IS NULL) AND (cover_audio_decision_id IS NOT NULL)) OR ((kind = 'profile'::text) AND (method = 'automatic'::text) AND (actor_user_id IS NULL) AND (selected_recording_mbid IS NULL) AND (cover_audio_decision_id IS NULL))))$constraint$ THEN RAISE EXCEPTION 'Existing policy constraint changed: shadify_match_kind_rules'; END IF;
END $preflight$;
ALTER TABLE shadify_ingest_match_decisions DROP CONSTRAINT shadify_ingest_match_decisions_method_check;
ALTER TABLE shadify_ingest_match_decisions ADD CONSTRAINT shadify_ingest_match_decisions_method_check
 CHECK(method IN ('automatic','reviewed','not_requested'));
ALTER TABLE shadify_ingest_match_decisions DROP CONSTRAINT shadify_match_kind_rules;
ALTER TABLE shadify_ingest_match_decisions ADD CONSTRAINT shadify_match_kind_rules CHECK(
 ((kind='audio' AND cover_audio_decision_id IS NULL AND
  ((method='automatic' AND actor_user_id IS NULL AND selected_recording_mbid IS NOT NULL) OR
   (method='reviewed' AND actor_user_id IS NOT NULL) OR
   (method='not_requested' AND actor_user_id IS NULL AND selected_recording_mbid IS NULL AND corrected_metadata='{}'::jsonb))) OR
  (kind='cover' AND method='automatic' AND actor_user_id IS NULL AND selected_recording_mbid IS NULL AND cover_audio_decision_id IS NOT NULL)) OR
 (kind='profile' AND method='automatic' AND actor_user_id IS NULL AND selected_recording_mbid IS NULL AND cover_audio_decision_id IS NULL));

ALTER TABLE shadify_ingest_approvals DROP CONSTRAINT shadify_ingest_approvals_match_decision_check;
ALTER TABLE shadify_ingest_approvals ADD CONSTRAINT shadify_ingest_approvals_match_decision_check CHECK(
 jsonb_typeof(match_decision)='object' AND match_decision ? 'decision'
 AND jsonb_typeof(match_decision->'decision')='string'
 AND match_decision->>'decision' IN ('automatic','reviewed','not_requested'));
DO $adapt$ DECLARE definition text; BEGIN
 SELECT pg_get_functiondef('validate_shadify_ingest_decision()'::regprocedure) INTO definition;
 IF (length(definition)-length(replace(definition,$anchor$ IF NEW.kind='profile' THEN$anchor$,'')))/length($anchor$ IF NEW.kind='profile' THEN$anchor$)<>1
 THEN RAISE EXCEPTION 'Exactly one reviewed decision branch required'; END IF;
 EXECUTE replace(definition,$anchor$ IF NEW.kind='profile' THEN$anchor$,$branch$ IF NEW.method='not_requested' THEN
  IF NEW.kind IS DISTINCT FROM 'audio' OR source_asset.asset_type IS DISTINCT FROM 'audio_original'
   OR source_asset.access_class IS DISTINCT FROM 'private' OR source_asset.publish_state IS DISTINCT FROM 'draft'
   OR NEW.actor_user_id IS NOT NULL OR NEW.selected_recording_mbid IS NOT NULL OR NEW.cover_audio_decision_id IS NOT NULL
   OR NEW.corrected_metadata IS DISTINCT FROM '{}'::jsonb
   OR evidence IS DISTINCT FROM '{"complete":true,"provider":"private_processing","policy":"private-audio-optional-metadata-v1","metadata_lookup":"not_requested"}'::jsonb
   OR NOT EXISTS(SELECT 1 FROM shadify_ingest_stages v JOIN shadify_ingest_stages e
    ON e.job_id=v.job_id AND e.input_sha256=v.input_sha256 AND e.scan_id=v.scan_id
    JOIN shadify_ingest_stages m ON m.id=NEW.matching_stage_id AND m.job_id=v.job_id AND m.input_sha256=v.input_sha256 AND m.scan_id=v.scan_id
    JOIN shadify_ingest_scans s ON s.id=v.scan_id AND s.job_id=v.job_id AND s.input_sha256=v.input_sha256
    WHERE v.job_id=NEW.job_id AND v.input_sha256=NEW.input_sha256
     AND v.stage='validation' AND v.payload @> '{"passed":true}'::jsonb
     AND e.stage='extraction' AND e.payload @> '{"complete":true}'::jsonb
     AND s.verdict='clean' AND s.full_coverage=true)
   THEN RAISE EXCEPTION 'Validated private audio policy evidence required'; END IF;
  RETURN NEW;
 END IF;
 IF NEW.kind='profile' THEN$branch$);
END $adapt$;
COMMIT;
