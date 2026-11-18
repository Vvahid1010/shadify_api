-- Additive only. Old media rows acquire no job, scan or approval.
BEGIN;
CREATE TABLE shadify_ingest_jobs (
 id uuid PRIMARY KEY,
 asset_id uuid NOT NULL UNIQUE REFERENCES shadify_media_assets(id) ON DELETE RESTRICT,
 input_sha256 text NOT NULL CHECK(input_sha256 ~ '^[a-f0-9]{64}$'),
 state text NOT NULL DEFAULT 'uploaded' CHECK(state IN
 ('uploaded','scanning','validating','matching','processing','ready','review_required','rejected')),
 revision bigint NOT NULL DEFAULT 1 CHECK(revision>0),
 last_error text CHECK(last_error IN ('scanner_unavailable','scanner_timeout','scanner_error',
 'scanner_evidence_invalid','bytes_changed','malware_detected')),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(id,input_sha256)
);
CREATE TABLE shadify_ingest_scans (
 id uuid PRIMARY KEY,
 job_id uuid NOT NULL,
 input_sha256 text NOT NULL,
 verdict text NOT NULL CHECK(verdict IN ('clean','infected','unknown')),
 engine_version text,
 definitions_version text,
 full_coverage boolean NOT NULL DEFAULT false,
 scanned_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 error_code text CHECK(error_code IN ('scanner_unavailable','scanner_timeout','scanner_error',
 'scanner_evidence_invalid','bytes_changed')),
 FOREIGN KEY(job_id,input_sha256) REFERENCES shadify_ingest_jobs(id,input_sha256) ON DELETE RESTRICT,
 CHECK((verdict='unknown' AND error_code IS NOT NULL) OR
 (verdict IN ('clean','infected') AND full_coverage=true AND error_code IS NULL AND
  engine_version IS NOT NULL AND definitions_version IS NOT NULL AND
  length(engine_version) BETWEEN 1 AND 256 AND length(definitions_version) BETWEEN 1 AND 256)),
 UNIQUE(id,job_id,input_sha256,verdict)
);
CREATE TRIGGER shadify_ingest_scan_immutable BEFORE UPDATE OR DELETE ON shadify_ingest_scans
 FOR EACH ROW EXECUTE FUNCTION protect_artist_ownership_audit();
CREATE TABLE shadify_ingest_approvals (
 asset_id uuid PRIMARY KEY REFERENCES shadify_media_assets(id) ON DELETE RESTRICT,
 job_id uuid NOT NULL REFERENCES shadify_ingest_jobs(id) ON DELETE RESTRICT,
 scan_id uuid NOT NULL,
 input_sha256 text NOT NULL,
 scan_verdict text NOT NULL DEFAULT 'clean' CHECK(scan_verdict='clean'),
 output_sha256 text NOT NULL CHECK(output_sha256 ~ '^[a-f0-9]{64}$'),
 storage_key text NOT NULL,
 storage_provider text NOT NULL CHECK(storage_provider='local'),
 mime text NOT NULL,
 size bigint NOT NULL CHECK(size>0),
 object_identity jsonb NOT NULL CHECK(jsonb_typeof(object_identity)='object'),
 validation jsonb NOT NULL CHECK(jsonb_typeof(validation)='object' AND validation @> '{"passed":true}'::jsonb),
 extraction jsonb NOT NULL CHECK(jsonb_typeof(extraction)='object' AND extraction @> '{"complete":true}'::jsonb),
 match_decision jsonb NOT NULL CHECK(jsonb_typeof(match_decision)='object' AND
   match_decision ? 'decision' AND jsonb_typeof(match_decision->'decision')='string' AND
   match_decision->>'decision' IN ('automatic','reviewed')),
 processing jsonb NOT NULL CHECK(jsonb_typeof(processing)='object' AND processing @> '{"complete":true}'::jsonb),
 approved_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 FOREIGN KEY(scan_id,job_id,input_sha256,scan_verdict)
 REFERENCES shadify_ingest_scans(id,job_id,input_sha256,verdict) ON DELETE RESTRICT
);
CREATE TRIGGER shadify_ingest_approval_immutable BEFORE UPDATE OR DELETE ON shadify_ingest_approvals
 FOR EACH ROW EXECUTE FUNCTION protect_artist_ownership_audit();
CREATE FUNCTION protect_shadify_ingest_job() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Ingest history cannot be deleted'; END IF;
 IF ROW(NEW.id,NEW.asset_id,NEW.input_sha256,NEW.created_at)
 IS DISTINCT FROM ROW(OLD.id,OLD.asset_id,OLD.input_sha256,OLD.created_at)
 OR NEW.revision<>OLD.revision+1 THEN RAISE EXCEPTION 'Ingest identity/hash/revision cannot rewrite'; END IF;
 IF OLD.state='rejected' OR (OLD.state='ready' AND NEW.state<>'rejected') THEN RAISE EXCEPTION 'Terminal ingest cannot rewrite'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER shadify_ingest_job_history BEFORE UPDATE OR DELETE ON shadify_ingest_jobs
 FOR EACH ROW EXECUTE FUNCTION protect_shadify_ingest_job();
CREATE FUNCTION require_shadify_ingest_ready_evidence() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NEW.state='ready' AND NOT EXISTS(SELECT 1 FROM shadify_ingest_approvals WHERE job_id=NEW.id)
 THEN RAISE EXCEPTION 'Clean scan alone is not ingest approval'; END IF;
 RETURN NEW;
END $$;
CREATE CONSTRAINT TRIGGER shadify_ingest_ready_evidence AFTER INSERT OR UPDATE ON shadify_ingest_jobs
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION require_shadify_ingest_ready_evidence();
COMMIT;
