-- Staged only: same quarantine/scanner/validation/approval pipeline. No grants.
BEGIN;
ALTER TABLE shadify_media_assets DROP CONSTRAINT shadify_profile_delivery_limits;
ALTER TABLE shadify_media_assets ADD CONSTRAINT shadify_profile_delivery_limits CHECK(
 asset_type NOT IN ('avatar_delivery','header_delivery') OR
 (mime IN ('image/png','image/avif') AND size<=10485760 AND access_class='free' AND publish_state='published'));
ALTER TABLE shadify_ingest_variants DROP CONSTRAINT shadify_ingest_variants_role_check;
ALTER TABLE shadify_ingest_variants ADD CONSTRAINT shadify_profile_variants_v2 CHECK(role IN ('master','high','low','cover','video','cover_small','cover_avif','cover_avif_small'));
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
  (output_role='video' AND (source_asset.asset_type<>'video_original' OR target_asset.asset_type<>'video_delivery' OR NEW.mime<>'video/mp4')) OR
  (output_role IN ('cover','cover_small','cover_avif','cover_avif_small') AND ((NEW.mime<>CASE WHEN output_role IN ('cover_avif','cover_avif_small') THEN 'image/avif' ELSE 'image/png' END) OR
   (source_asset.asset_type IN ('avatar_original','header_original') AND
    (decision.kind<>'profile' OR target_asset.asset_type<>split_part(source_asset.asset_type,'_',1)||'_delivery' OR NEW.size>10485760)) OR
   (source_asset.asset_type NOT IN ('avatar_original','header_original') AND (target_asset.asset_type<>'artwork_delivery' OR output_role<>'cover'))))
  THEN RAISE EXCEPTION 'Master/variant semantics required'; END IF;
 RETURN NEW;
END $$;
CREATE OR REPLACE FUNCTION require_shadify_pipeline_ready() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE kind text; required text[]; version text;
BEGIN
 IF NEW.state='ready' THEN
  SELECT asset_type INTO kind FROM shadify_media_assets WHERE id=NEW.asset_id;
  SELECT payload->>'pipeline_version' INTO version FROM shadify_ingest_stages WHERE job_id=NEW.id AND stage='prepared_outputs';
  required:=CASE WHEN kind='video_original' THEN ARRAY['master','video'] WHEN kind='audio_original' THEN ARRAY['master','high','low'] WHEN kind IN ('avatar_original','header_original') AND version='profile-image-v2' THEN ARRAY['master','cover','cover_small','cover_avif','cover_avif_small'] ELSE ARRAY['master','cover'] END;
  IF (SELECT array_agg(v.role ORDER BY v.role) FROM shadify_ingest_variants v JOIN shadify_ingest_approvals a ON a.asset_id=v.asset_id AND a.job_id=v.job_id AND a.output_sha256=v.output_sha256 WHERE v.job_id=NEW.id) IS DISTINCT FROM (SELECT array_agg(x ORDER BY x) FROM unnest(required) x) THEN RAISE EXCEPTION 'All required approved variants must be ready'; END IF;
 END IF;
 RETURN NEW;
END $$;
COMMIT;
