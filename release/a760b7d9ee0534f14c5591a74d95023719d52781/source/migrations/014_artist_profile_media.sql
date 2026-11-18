-- Artist avatar/header only. No data rewrite, grants, secrets or activation.
BEGIN;
ALTER TABLE shadify_media_assets ALTER COLUMN track_id DROP NOT NULL;
ALTER TABLE shadify_media_assets DROP CONSTRAINT shadify_media_assets_asset_type_check;
ALTER TABLE shadify_media_assets ADD CONSTRAINT shadify_media_assets_asset_type_check CHECK(asset_type IN
 ('audio_original','artwork_original','audio_delivery','audio_preview','artwork_delivery','video_delivery',
  'avatar_original','header_original','avatar_delivery','header_delivery'));
ALTER TABLE shadify_media_assets ADD CONSTRAINT shadify_profile_scope CHECK(
 (asset_type IN ('avatar_original','header_original','avatar_delivery','header_delivery') AND track_id IS NULL)
 OR (asset_type NOT IN ('avatar_original','header_original','avatar_delivery','header_delivery') AND track_id IS NOT NULL));
ALTER TABLE shadify_media_assets ADD CONSTRAINT shadify_profile_original_private CHECK(
 asset_type NOT IN ('avatar_original','header_original') OR (access_class='private' AND publish_state='draft'
  AND mime IN ('image/png','image/jpeg') AND size<=10485760));
ALTER TABLE shadify_media_assets ADD CONSTRAINT shadify_profile_delivery_limits CHECK(
 asset_type NOT IN ('avatar_delivery','header_delivery') OR
 (mime='image/png' AND size<=10485760 AND access_class='free' AND publish_state='published'));
ALTER TABLE shadify_ingest_match_decisions DROP CONSTRAINT shadify_ingest_match_decisions_kind_check;
ALTER TABLE shadify_ingest_match_decisions ADD CONSTRAINT shadify_ingest_match_decisions_kind_check CHECK(kind IN ('audio','cover','profile'));
DO $$ DECLARE item record; found integer:=0; BEGIN
 FOR item IN SELECT conname FROM pg_catalog.pg_constraint
  WHERE conrelid='shadify_ingest_match_decisions'::regclass AND contype='c'
  AND pg_catalog.pg_get_constraintdef(oid) LIKE '%cover_audio_decision_id%' LOOP
  found:=found+1; EXECUTE format('ALTER TABLE shadify_ingest_match_decisions DROP CONSTRAINT %I',item.conname);
 END LOOP;
 IF found<>1 THEN RAISE EXCEPTION 'Exactly one existing kind rules constraint required'; END IF;
END $$;
ALTER TABLE shadify_ingest_match_decisions ADD CONSTRAINT shadify_match_kind_rules CHECK(
 ((kind='audio' AND cover_audio_decision_id IS NULL AND
  ((method='automatic' AND actor_user_id IS NULL AND selected_recording_mbid IS NOT NULL) OR
   (method='reviewed' AND actor_user_id IS NOT NULL))) OR
  (kind='cover' AND method='automatic' AND actor_user_id IS NULL AND selected_recording_mbid IS NULL AND cover_audio_decision_id IS NOT NULL)) OR
 (kind='profile' AND method='automatic' AND actor_user_id IS NULL AND selected_recording_mbid IS NULL AND cover_audio_decision_id IS NULL));
CREATE OR REPLACE FUNCTION validate_shadify_ingest_decision() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE evidence jsonb; source_asset shadify_media_assets; audio_asset shadify_media_assets; audio_job shadify_ingest_jobs;
BEGIN
 SELECT payload INTO evidence FROM shadify_ingest_stages WHERE id=NEW.matching_stage_id AND stage='matching';
 IF evidence IS NULL OR NOT (evidence @> '{"complete":true}'::jsonb) THEN RAISE EXCEPTION 'Completed matching snapshot required'; END IF;
 SELECT a.* INTO source_asset FROM shadify_media_assets a JOIN shadify_ingest_jobs j ON j.asset_id=a.id WHERE j.id=NEW.job_id;
 IF NEW.kind='profile' THEN
  IF source_asset.asset_type NOT IN ('avatar_original','header_original') OR source_asset.track_id IS NOT NULL
   OR source_asset.mime NOT IN ('image/png','image/jpeg') OR source_asset.size>10485760
   OR evidence->>'provider' IS DISTINCT FROM 'artist_profile'
   OR evidence->>'purpose' IS DISTINCT FROM split_part(source_asset.asset_type,'_',1)
   OR NOT EXISTS(SELECT 1 FROM shadify_ingest_stages WHERE job_id=NEW.job_id AND stage='validation' AND payload @> '{"passed":true}'::jsonb)
   OR NOT EXISTS(SELECT 1 FROM shadify_ingest_stages WHERE job_id=NEW.job_id AND stage='extraction' AND payload @> '{"complete":true}'::jsonb)
   THEN RAISE EXCEPTION 'Validated artist-scoped profile image evidence required'; END IF;
  RETURN NEW;
 END IF;
 IF NEW.kind='audio' THEN
  IF source_asset.asset_type<>'audio_original' OR evidence->>'provider'<>'musicbrainz' THEN RAISE EXCEPTION 'Audio lookup required'; END IF;
  IF NEW.selected_recording_mbid IS NOT NULL AND NOT EXISTS(SELECT 1 FROM jsonb_array_elements(evidence->'candidates') x WHERE x->>'mbid'=NEW.selected_recording_mbid::text)
   THEN RAISE EXCEPTION 'Candidate not in server snapshot'; END IF;
  IF NEW.method='automatic' AND evidence->>'automatic_mbid' IS DISTINCT FROM NEW.selected_recording_mbid::text
   THEN RAISE EXCEPTION 'Confident automatic evidence required'; END IF;
 ELSE
  SELECT j.* INTO audio_job FROM shadify_ingest_jobs j JOIN shadify_ingest_match_decisions d ON d.job_id=j.id
   WHERE d.id=NEW.cover_audio_decision_id AND d.kind='audio';
  SELECT * INTO audio_asset FROM shadify_media_assets WHERE id=audio_job.asset_id;
  IF source_asset.asset_type<>'artwork_original' OR audio_job.state<>'ready' OR audio_asset.artist_id IS DISTINCT FROM source_asset.artist_id
   OR audio_asset.track_id IS DISTINCT FROM source_asset.track_id OR evidence->>'audio_decision_id' IS DISTINCT FROM NEW.cover_audio_decision_id::text
   THEN RAISE EXCEPTION 'Cover requires same-track ready audio decision'; END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE OR REPLACE FUNCTION require_shadify_pipeline_approval() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE prepared jsonb; output jsonb; output_role text; source_asset shadify_media_assets; target_asset shadify_media_assets; decision shadify_ingest_match_decisions;
BEGIN
 IF NOT EXISTS(SELECT 1 FROM shadify_ingest_stages WHERE job_id=NEW.job_id AND input_sha256=NEW.input_sha256
  AND scan_id=NEW.scan_id AND stage='validation' AND payload @> '{"passed":true}') OR
 NOT EXISTS(SELECT 1 FROM shadify_ingest_stages WHERE job_id=NEW.job_id AND input_sha256=NEW.input_sha256
  AND scan_id=NEW.scan_id AND stage='extraction' AND payload @> '{"complete":true}') OR
 NOT EXISTS(SELECT 1 FROM shadify_ingest_stages WHERE job_id=NEW.job_id AND input_sha256=NEW.input_sha256
  AND scan_id=NEW.scan_id AND stage='processing' AND payload @> '{"complete":true}') THEN RAISE EXCEPTION 'All pipeline stages required'; END IF;
 SELECT * INTO decision FROM shadify_ingest_match_decisions WHERE job_id=NEW.job_id AND input_sha256=NEW.input_sha256;
 IF decision.id IS NULL OR NEW.match_decision->>'decision_id' IS DISTINCT FROM decision.id::text OR
  NEW.match_decision->>'decision' IS DISTINCT FROM decision.method THEN RAISE EXCEPTION 'Recorded decision required'; END IF;
 SELECT payload INTO prepared FROM shadify_ingest_stages WHERE job_id=NEW.job_id AND stage='prepared_outputs';
 SELECT key,value INTO output_role,output FROM jsonb_each(prepared->'outputs') WHERE value->>'asset_id'=NEW.asset_id::text;
 SELECT * INTO target_asset FROM shadify_media_assets WHERE id=NEW.asset_id;
 SELECT a.* INTO source_asset FROM shadify_media_assets a JOIN shadify_ingest_jobs j ON j.asset_id=a.id WHERE j.id=NEW.job_id;
 IF output IS NULL OR output->>'sha256' IS DISTINCT FROM NEW.output_sha256 OR output->>'key' IS DISTINCT FROM NEW.storage_key
  OR output->>'mime' IS DISTINCT FROM NEW.mime OR (output->>'size')::bigint IS DISTINCT FROM NEW.size
  OR target_asset.artist_id IS DISTINCT FROM source_asset.artist_id OR target_asset.track_id IS DISTINCT FROM source_asset.track_id
  OR target_asset.key IS DISTINCT FROM NEW.storage_key OR target_asset.mime IS DISTINCT FROM NEW.mime OR target_asset.size IS DISTINCT FROM NEW.size
  OR target_asset.storage_provider<>'local' OR target_asset.state<>'ready' OR NEW.storage_key LIKE 'quarantine/%'
 THEN RAISE EXCEPTION 'Prepared output identity required'; END IF;
 IF target_asset.owner_id IS DISTINCT FROM source_asset.owner_id OR
  (output_role='master' AND (NEW.output_sha256<>NEW.input_sha256 OR NEW.mime<>source_asset.mime OR NEW.size<>source_asset.size OR target_asset.asset_type<>source_asset.asset_type)) OR
  (output_role IN ('high','low') AND (target_asset.asset_type<>'audio_delivery' OR NEW.mime<>'audio/mpeg')) OR
  (output_role='cover' AND (NEW.mime<>'image/png' OR
   (source_asset.asset_type IN ('avatar_original','header_original') AND
    (decision.kind<>'profile' OR target_asset.asset_type<>split_part(source_asset.asset_type,'_',1)||'_delivery' OR NEW.size>10485760)) OR
   (source_asset.asset_type NOT IN ('avatar_original','header_original') AND target_asset.asset_type<>'artwork_delivery')))
  THEN RAISE EXCEPTION 'Master/variant semantics required'; END IF;
 RETURN NEW;
END $$;
COMMIT;
