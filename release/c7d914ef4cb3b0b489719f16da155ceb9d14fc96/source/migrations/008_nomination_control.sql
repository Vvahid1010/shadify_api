-- Operational finalized v1. Earlier007 preparation remains inert; no import/grant.
BEGIN;
CREATE TABLE shadify_admin_nomination_v1 (
 id uuid PRIMARY KEY,
 scope jsonb NOT NULL,
 revision integer NOT NULL DEFAULT 1 CHECK (revision>0),
 state text NOT NULL CHECK (state IN ('pending','claimed','revoked','expired','blocked','replaced')),
 intent text NOT NULL CHECK (intent IN ('initialize','recover')),
 expected_admin_revision integer NOT NULL CHECK (expected_admin_revision>=0),
 expected_primary_account_id varchar(36),
 delegate_policy text CHECK (delegate_policy IN ('retain','revoke')),
 email_tags text[] NOT NULL DEFAULT '{}',
 phone_tags text[] NOT NULL DEFAULT '{}',
 existing_account_id varchar(36),
 expires_at timestamptz NOT NULL,
 predecessor_id uuid REFERENCES shadify_admin_nomination_v1(id) ON DELETE RESTRICT,
 claimed_user_id uuid REFERENCES shadify_users(id) ON DELETE RESTRICT,
 CHECK (cardinality(email_tags)<=64 AND cardinality(phone_tags)<=64 AND cardinality(email_tags)+cardinality(phone_tags)>0),
 CHECK ((intent='initialize' AND expected_admin_revision=0 AND expected_primary_account_id IS NULL AND delegate_policy IS NULL)
     OR (intent='recover' AND expected_admin_revision>0 AND expected_primary_account_id IS NOT NULL AND delegate_policy IS NOT NULL)),
 CHECK ((state='claimed')=(claimed_user_id IS NOT NULL))
);
CREATE TABLE shadify_nomination_control (
 singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
 nomination_id uuid REFERENCES shadify_admin_nomination_v1(id) ON DELETE RESTRICT
);
INSERT INTO shadify_nomination_control(singleton) VALUES(true);
CREATE FUNCTION protect_shadify_nomination_v1() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Nomination history cannot be deleted'; END IF;
 IF OLD.state<>'pending' THEN RAISE EXCEPTION 'Terminal nomination cannot change'; END IF;
 IF OLD.expires_at<=clock_timestamp() AND NEW.state='pending' THEN RAISE EXCEPTION 'Expired nomination cannot renew'; END IF;
 IF ROW(NEW.intent,NEW.expected_admin_revision,NEW.expected_primary_account_id,NEW.delegate_policy,NEW.email_tags,NEW.phone_tags,NEW.existing_account_id,NEW.predecessor_id)
    IS DISTINCT FROM ROW(OLD.intent,OLD.expected_admin_revision,OLD.expected_primary_account_id,OLD.delegate_policy,OLD.email_tags,OLD.phone_tags,OLD.existing_account_id,OLD.predecessor_id)
 THEN RAISE EXCEPTION 'Reviewed nomination selection cannot rewrite'; END IF;
 IF NEW.state='pending' AND NEW.expires_at<=OLD.expires_at THEN RAISE EXCEPTION 'Renewal must extend expiry'; END IF;
 IF NEW.id<>OLD.id OR NEW.scope<>OLD.scope OR NEW.revision<>OLD.revision+1
 THEN RAISE EXCEPTION 'Nomination scope/id/revision cannot rewrite'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER shadify_nomination_v1_history BEFORE UPDATE OR DELETE ON shadify_admin_nomination_v1
 FOR EACH ROW EXECUTE FUNCTION protect_shadify_nomination_v1();
CREATE TABLE shadify_nomination_operations (
 operation_id uuid PRIMARY KEY,
 nomination_id uuid NOT NULL REFERENCES shadify_admin_nomination_v1(id) ON DELETE RESTRICT,
 scope jsonb NOT NULL,
 operation text NOT NULL,
 actor_account_id varchar(36) NOT NULL,
 body_sha256 text NOT NULL CHECK(body_sha256 ~ '^[a-f0-9]{64}$'),
 approval_fingerprint text NOT NULL UNIQUE CHECK(approval_fingerprint ~ '^[a-f0-9]{64}$'),
 result jsonb NOT NULL,
 occurred_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TRIGGER shadify_nomination_operations_immutable BEFORE UPDATE OR DELETE ON shadify_nomination_operations
 FOR EACH ROW EXECUTE FUNCTION protect_artist_ownership_audit();
CREATE VIEW shadify_nomination_operation_status AS
 SELECT o.operation_id,o.scope,o.operation,o.actor_account_id,o.body_sha256,o.result,
 n.id AS nomination_id,n.revision,
 CASE WHEN n.state='pending' AND n.expires_at<=clock_timestamp() THEN 'expired' ELSE n.state END AS state,
 n.expires_at FROM shadify_nomination_operations o JOIN shadify_admin_nomination_v1 n ON n.id=o.nomination_id;
COMMIT;
