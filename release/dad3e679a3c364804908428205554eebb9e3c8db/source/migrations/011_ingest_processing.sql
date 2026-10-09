-- Forward only. No historical approvals or pipeline stages are inferred.
BEGIN;
ALTER TABLE shadify_ingest_attempts ADD COLUMN stage text NOT NULL DEFAULT 'scan'
 CHECK(stage IN ('scan','validation','matching','processing'));
CREATE FUNCTION protect_shadify_ingest_attempt_stage() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN IF NEW.stage<>OLD.stage THEN RAISE EXCEPTION 'Attempt stage cannot rewrite'; END IF; RETURN NEW; END $$;
CREATE TRIGGER shadify_ingest_attempt_stage BEFORE UPDATE ON shadify_ingest_attempts
 FOR EACH ROW EXECUTE FUNCTION protect_shadify_ingest_attempt_stage();
ALTER TABLE shadify_ingest_jobs DROP CONSTRAINT shadify_ingest_jobs_last_error_check;
ALTER TABLE shadify_ingest_jobs ADD CHECK(last_error IN
 ('scanner_unavailable','scanner_timeout','scanner_error','scanner_evidence_invalid','bytes_changed','malware_detected',
 'validation_invalid','metadata_invalid','provider_unavailable','match_ambiguous','processing_unavailable',
 'processing_timeout','processing_error','output_invalid','pipeline_evidence_invalid'));
CREATE TABLE shadify_ingest_stages (
 id uuid PRIMARY KEY,job_id uuid NOT NULL,input_sha256 text NOT NULL,scan_id uuid NOT NULL,
 scan_verdict text NOT NULL DEFAULT 'clean' CHECK(scan_verdict='clean'),
 stage text NOT NULL CHECK(stage IN ('validation','extraction','matching','prepared_outputs','processing')),
 payload jsonb NOT NULL CHECK(jsonb_typeof(payload)='object' AND octet_length(payload::text)<=65536),
 recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),UNIQUE(job_id,stage),UNIQUE(id,job_id,input_sha256),
 FOREIGN KEY(job_id,input_sha256) REFERENCES shadify_ingest_jobs(id,input_sha256),
 FOREIGN KEY(scan_id,job_id,input_sha256,scan_verdict) REFERENCES shadify_ingest_scans(id,job_id,input_sha256,verdict)
);
CREATE TRIGGER shadify_ingest_stage_history BEFORE UPDATE OR DELETE ON shadify_ingest_stages
 FOR EACH ROW EXECUTE FUNCTION protect_artist_ownership_audit();
CREATE TABLE shadify_ingest_match_decisions (
 id uuid PRIMARY KEY,job_id uuid NOT NULL UNIQUE,input_sha256 text NOT NULL,matching_stage_id uuid NOT NULL,
 kind text NOT NULL CHECK(kind IN ('audio','cover')),method text NOT NULL CHECK(method IN ('automatic','reviewed')),
 selected_recording_mbid uuid,cover_audio_decision_id uuid REFERENCES shadify_ingest_match_decisions(id),
 corrected_metadata jsonb NOT NULL CHECK(jsonb_typeof(corrected_metadata)='object' AND octet_length(corrected_metadata::text)<=8192),
 reason text NOT NULL CHECK(length(reason) BETWEEN 1 AND 1000),actor_user_id uuid REFERENCES shadify_users(id),
 decided_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 FOREIGN KEY(matching_stage_id,job_id,input_sha256) REFERENCES shadify_ingest_stages(id,job_id,input_sha256),
 CHECK((kind='audio' AND cover_audio_decision_id IS NULL AND
  ((method='automatic' AND actor_user_id IS NULL AND selected_recording_mbid IS NOT NULL) OR
   (method='reviewed' AND actor_user_id IS NOT NULL))) OR
  (kind='cover' AND method='automatic' AND actor_user_id IS NULL AND selected_recording_mbid IS NULL AND cover_audio_decision_id IS NOT NULL))
);
CREATE TRIGGER shadify_ingest_match_history BEFORE UPDATE OR DELETE ON shadify_ingest_match_decisions
 FOR EACH ROW EXECUTE FUNCTION protect_artist_ownership_audit();
CREATE FUNCTION validate_shadify_ingest_decision() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE evidence jsonb; source_asset shadify_media_assets; audio_asset shadify_media_assets; audio_job shadify_ingest_jobs;
BEGIN
 SELECT payload INTO evidence FROM shadify_ingest_stages WHERE id=NEW.matching_stage_id AND stage='matching';
 IF evidence IS NULL OR NOT (evidence @> '{"complete":true}'::jsonb) THEN RAISE EXCEPTION 'Completed matching snapshot required'; END IF;
 SELECT a.* INTO source_asset FROM shadify_media_assets a JOIN shadify_ingest_jobs j ON j.asset_id=a.id WHERE j.id=NEW.job_id;
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
CREATE TRIGGER shadify_ingest_decision_provenance BEFORE INSERT ON shadify_ingest_match_decisions
 FOR EACH ROW EXECUTE FUNCTION validate_shadify_ingest_decision();
CREATE TABLE shadify_ingest_variants (
 job_id uuid NOT NULL REFERENCES shadify_ingest_jobs(id),role text NOT NULL CHECK(role IN ('master','high','low','cover')),
 asset_id uuid NOT NULL UNIQUE REFERENCES shadify_media_assets(id),
 output_sha256 text NOT NULL CHECK(output_sha256 ~ '^[a-f0-9]{64}$'),source_quality jsonb NOT NULL CHECK(jsonb_typeof(source_quality)='object'),
 pipeline_version text NOT NULL CHECK(length(pipeline_version) BETWEEN 1 AND 128),PRIMARY KEY(job_id,role)
);
CREATE TRIGGER shadify_ingest_variant_history BEFORE UPDATE OR DELETE ON shadify_ingest_variants
 FOR EACH ROW EXECUTE FUNCTION protect_artist_ownership_audit();
CREATE FUNCTION validate_shadify_ingest_variant() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE manifest jsonb; source_quality jsonb;
BEGIN
 SELECT payload INTO manifest FROM shadify_ingest_stages WHERE job_id=NEW.job_id AND stage='prepared_outputs';
 SELECT payload->'source_quality' INTO source_quality FROM shadify_ingest_stages WHERE job_id=NEW.job_id AND stage='validation';
 IF manifest IS NULL OR manifest->'outputs'->NEW.role->>'asset_id' IS DISTINCT FROM NEW.asset_id::text OR
  manifest->'outputs'->NEW.role->>'sha256' IS DISTINCT FROM NEW.output_sha256 OR
  manifest->>'pipeline_version' IS DISTINCT FROM NEW.pipeline_version OR source_quality IS DISTINCT FROM NEW.source_quality
  THEN RAISE EXCEPTION 'Variant role must match prepared pipeline output'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER shadify_ingest_variant_provenance BEFORE INSERT ON shadify_ingest_variants
 FOR EACH ROW EXECUTE FUNCTION validate_shadify_ingest_variant();
CREATE FUNCTION require_shadify_pipeline_approval() RETURNS trigger LANGUAGE plpgsql AS $$
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
  (output_role='cover' AND (target_asset.asset_type<>'artwork_delivery' OR NEW.mime<>'image/png'))
  THEN RAISE EXCEPTION 'Master/variant semantics required'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER shadify_ingest_pipeline_approval BEFORE INSERT ON shadify_ingest_approvals
 FOR EACH ROW EXECUTE FUNCTION require_shadify_pipeline_approval();
CREATE FUNCTION require_shadify_pipeline_ready() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE kind text; required text[];
BEGIN
 IF NEW.state='ready' THEN
  SELECT asset_type INTO kind FROM shadify_media_assets WHERE id=NEW.asset_id;
  required:=CASE WHEN kind='audio_original' THEN ARRAY['master','high','low'] ELSE ARRAY['master','cover'] END;
  IF (SELECT array_agg(v.role ORDER BY v.role) FROM shadify_ingest_variants v JOIN shadify_ingest_approvals a
    ON a.asset_id=v.asset_id AND a.job_id=v.job_id AND a.output_sha256=v.output_sha256 WHERE v.job_id=NEW.id)
   IS DISTINCT FROM (SELECT array_agg(x ORDER BY x) FROM unnest(required) x)
   THEN RAISE EXCEPTION 'Complete approved variants required'; END IF;
 END IF;RETURN NEW;
END $$;
CREATE CONSTRAINT TRIGGER shadify_ingest_pipeline_ready AFTER INSERT OR UPDATE ON shadify_ingest_jobs
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION require_shadify_pipeline_ready();
COMMIT;
