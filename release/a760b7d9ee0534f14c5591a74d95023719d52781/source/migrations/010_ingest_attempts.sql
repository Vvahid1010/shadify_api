-- Forward only: preserve009 and its historical scan/approval evidence.
BEGIN;
CREATE TABLE shadify_ingest_attempts (
 id uuid PRIMARY KEY,
 job_id uuid NOT NULL,
 input_sha256 text NOT NULL,
 claimed_revision bigint NOT NULL CHECK(claimed_revision>1),
 started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 lease_expires_at timestamptz NOT NULL CHECK(lease_expires_at>started_at),
 finished_at timestamptz,
 outcome text CHECK(outcome IN ('completed','abandoned')),
 FOREIGN KEY(job_id,input_sha256) REFERENCES shadify_ingest_jobs(id,input_sha256) ON DELETE RESTRICT,
 UNIQUE(job_id,claimed_revision),
 CHECK((finished_at IS NULL AND outcome IS NULL) OR
       (finished_at IS NOT NULL AND outcome IS NOT NULL AND finished_at>=started_at))
);
CREATE UNIQUE INDEX shadify_ingest_one_attempt ON shadify_ingest_attempts(job_id) WHERE finished_at IS NULL;
CREATE FUNCTION protect_shadify_ingest_attempt() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Ingest attempts cannot be deleted'; END IF;
 IF OLD.finished_at IS NOT NULL OR NEW.finished_at IS NULL OR
 ROW(NEW.id,NEW.job_id,NEW.input_sha256,NEW.claimed_revision,NEW.started_at,NEW.lease_expires_at)
 IS DISTINCT FROM ROW(OLD.id,OLD.job_id,OLD.input_sha256,OLD.claimed_revision,OLD.started_at,OLD.lease_expires_at)
 THEN RAISE EXCEPTION 'Ingest attempt cannot rewrite'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER shadify_ingest_attempt_history BEFORE UPDATE OR DELETE ON shadify_ingest_attempts
 FOR EACH ROW EXECUTE FUNCTION protect_shadify_ingest_attempt();
COMMIT;
