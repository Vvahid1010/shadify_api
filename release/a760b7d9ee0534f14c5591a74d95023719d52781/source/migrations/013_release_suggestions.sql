-- Forward source-only proposal history; no closed ingest stages are rewritten.
BEGIN;
CREATE TABLE shadify_ingest_release_proposals (
 id uuid PRIMARY KEY,job_id uuid NOT NULL,input_sha256 text NOT NULL,
 extraction_stage_id uuid NOT NULL,recording_decision_id uuid NOT NULL,
 artist_mbid uuid NOT NULL,album text NOT NULL CHECK(length(album) BETWEEN 1 AND 200),
 request_key text NOT NULL CHECK(request_key ~ '^[0-9a-f]{64}$'),request jsonb NOT NULL,
 last_reconciled_at timestamptz,
 format_version integer NOT NULL CHECK(format_version=1),created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 result jsonb,response_sha256 text CHECK(response_sha256 ~ '^[0-9a-f]{64}$'),provider_fetched_at timestamptz,recorded_at timestamptz,
 UNIQUE(job_id,format_version),FOREIGN KEY(job_id,input_sha256) REFERENCES shadify_ingest_jobs(id,input_sha256),
 FOREIGN KEY(extraction_stage_id,job_id,input_sha256) REFERENCES shadify_ingest_stages(id,job_id,input_sha256),
 FOREIGN KEY(recording_decision_id) REFERENCES shadify_ingest_match_decisions(id),
 CHECK(octet_length(request::text)<=4096),CHECK(result IS NULL OR octet_length(result::text)<=8192),
 CHECK((result IS NULL AND response_sha256 IS NULL AND provider_fetched_at IS NULL AND recorded_at IS NULL) OR
       (result IS NOT NULL AND response_sha256 IS NOT NULL AND provider_fetched_at IS NOT NULL AND recorded_at IS NOT NULL))
);
CREATE INDEX shadify_release_pending ON shadify_ingest_release_proposals(last_reconciled_at NULLS FIRST,created_at,id) WHERE result IS NULL;
CREATE FUNCTION protect_shadify_release_proposal() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE e jsonb; m jsonb; d shadify_ingest_match_decisions; candidate jsonb; item jsonb; total integer; n integer;
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Release proposal history immutable'; END IF;
 IF TG_OP='UPDATE' AND (OLD.result IS NOT NULL OR
  (to_jsonb(NEW)-ARRAY['result','response_sha256','provider_fetched_at','recorded_at','last_reconciled_at']) IS DISTINCT FROM
  (to_jsonb(OLD)-ARRAY['result','response_sha256','provider_fetched_at','recorded_at','last_reconciled_at'])) THEN RAISE EXCEPTION 'Release source/history immutable'; END IF;
 SELECT payload INTO e FROM shadify_ingest_stages WHERE id=NEW.extraction_stage_id AND job_id=NEW.job_id AND input_sha256=NEW.input_sha256 AND stage='extraction';
 SELECT * INTO d FROM shadify_ingest_match_decisions WHERE id=NEW.recording_decision_id AND job_id=NEW.job_id AND input_sha256=NEW.input_sha256 AND kind='audio';
 SELECT payload INTO m FROM shadify_ingest_stages WHERE id=d.matching_stage_id AND job_id=NEW.job_id AND input_sha256=NEW.input_sha256 AND stage='matching';
 IF e IS NULL OR e->'complete' IS DISTINCT FROM 'true'::jsonb OR m IS NULL OR m->'format_version' IS DISTINCT FROM '2'::jsonb OR m->'complete' IS DISTINCT FROM 'true'::jsonb OR m->>'provider' IS DISTINCT FROM 'musicbrainz' OR m->'truncated' IS DISTINCT FROM 'false'::jsonb OR jsonb_typeof(m->'candidates') IS DISTINCT FROM 'array' OR m->'total' IS DISTINCT FROM to_jsonb(jsonb_array_length(m->'candidates')) OR d.selected_recording_mbid IS NULL THEN RAISE EXCEPTION 'Release source invalid'; END IF;
 SELECT v INTO candidate FROM jsonb_array_elements(m->'candidates') v WHERE v->>'mbid'=d.selected_recording_mbid::text;
 IF candidate IS NULL OR jsonb_array_length(candidate->'artist_credits') IS DISTINCT FROM 1 OR
  candidate->'artist_credits'->0->>'id' IS DISTINCT FROM NEW.artist_mbid::text OR
  NEW.album IS DISTINCT FROM btrim(COALESCE(d.corrected_metadata->>'album',e->'metadata'->>'album','')) OR
  NEW.request IS DISTINCT FROM jsonb_build_object('resource','release_suggestions','format_version',1,'query',jsonb_build_object('album',NEW.album,'artist_mbid',NEW.artist_mbid::text),'limit',5,'page',0)
 THEN RAISE EXCEPTION 'Release artist/query invalid'; END IF;
 -- SHA256 of sorted-key compact UTF-8 typed request; JSON escape album.
 IF NEW.request_key IS DISTINCT FROM encode(sha256(convert_to(
  '{"format_version":1,"limit":5,"page":0,"query":{"album":'||to_json(NEW.album)::text||
  ',"artist_mbid":'||to_json(NEW.artist_mbid::text)::text||'},"resource":"release_suggestions"}', 'UTF8')),'hex')
 THEN RAISE EXCEPTION 'Release request key invalid'; END IF;
 IF NEW.result IS NOT NULL THEN
  IF jsonb_typeof(NEW.result) IS DISTINCT FROM 'object' OR
   NOT (NEW.result ?& ARRAY['candidates','total','page_complete','search_complete','has_more']) OR
   (NEW.result-ARRAY['candidates','total','page_complete','search_complete','has_more'])<>'{}'::jsonb OR
   jsonb_typeof(NEW.result->'total') IS DISTINCT FROM 'number' OR NOT (NEW.result->>'total' ~ '^[0-9]{1,9}$') OR
   jsonb_typeof(NEW.result->'candidates') IS DISTINCT FROM 'array' THEN RAISE EXCEPTION 'Release result invalid'; END IF;
  total=(NEW.result->>'total')::integer;n=jsonb_array_length(NEW.result->'candidates');
  IF n<>LEAST(total,5) OR NEW.result->'page_complete' IS DISTINCT FROM 'true'::jsonb OR
   NEW.result->'has_more' IS DISTINCT FROM to_jsonb(total>5) OR NEW.result->'search_complete' IS DISTINCT FROM to_jsonb(total<=5)
  THEN RAISE EXCEPTION 'Release page invalid'; END IF;
  FOR item IN SELECT v FROM jsonb_array_elements(NEW.result->'candidates') v LOOP
   IF jsonb_typeof(item) IS DISTINCT FROM 'object' OR NOT(item ?& ARRAY['external_release_id','title','artist_credit','artist_mbid']) OR
    (item-ARRAY['external_release_id','title','artist_credit','artist_mbid'])<>'{}'::jsonb OR
    jsonb_typeof(item->'title') IS DISTINCT FROM 'string' OR length(item->>'title') NOT BETWEEN 1 AND 200 OR (item->>'title') ~ '[[:cntrl:]]' OR
    jsonb_typeof(item->'artist_credit') IS DISTINCT FROM 'string' OR length(item->>'artist_credit') NOT BETWEEN 1 AND 120 OR (item->>'artist_credit') ~ '[[:cntrl:]]' OR
    item->>'artist_mbid' IS DISTINCT FROM NEW.artist_mbid::text OR (item->>'external_release_id')::uuid::text IS DISTINCT FROM item->>'external_release_id'
   THEN RAISE EXCEPTION 'Release candidate invalid'; END IF;
  END LOOP;
  IF (SELECT count(DISTINCT v->>'external_release_id') FROM jsonb_array_elements(NEW.result->'candidates') v)<>n THEN RAISE EXCEPTION 'Release candidates duplicate'; END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER shadify_release_history BEFORE INSERT OR UPDATE OR DELETE ON shadify_ingest_release_proposals FOR EACH ROW EXECUTE FUNCTION protect_shadify_release_proposal();
COMMIT;
