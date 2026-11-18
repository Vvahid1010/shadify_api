-- Additive nomination core preparation only; no automatic grant/import/claim.
-- Not selected by runtime readiness or native package target until Auth contract review.
BEGIN;
CREATE TABLE shadify_admin_nominations (
 id uuid PRIMARY KEY,
 app_instance_id uuid NOT NULL,
 deployment text NOT NULL CHECK (length(deployment) BETWEEN 1 AND 128),
 revision bigint NOT NULL DEFAULT 1 CHECK (revision > 0),
 state text NOT NULL CHECK (state IN ('pending','revoked','claimed','expired')),
 mode text NOT NULL CHECK (mode IN ('initialize_primary','recover_primary')),
 email_tag text NOT NULL CHECK (length(email_tag) BETWEEN 1 AND 1024),
 phone_tag text NOT NULL CHECK (length(phone_tag) BETWEEN 1 AND 1024),
 expected_admin_revision bigint NOT NULL CHECK (expected_admin_revision >= 0),
 expected_primary_account_id text,
 delegates text CHECK (delegates IN ('retain','revoke')),
 expires_at timestamptz NOT NULL,
 claimed_user_id uuid REFERENCES shadify_users(id) ON DELETE RESTRICT,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 CHECK ((mode='initialize_primary' AND expected_primary_account_id IS NULL AND delegates IS NULL)
     OR (mode='recover_primary' AND expected_primary_account_id IS NOT NULL AND delegates IS NOT NULL)),
 CHECK ((state='claimed') = (claimed_user_id IS NOT NULL))
);
CREATE UNIQUE INDEX shadify_one_pending_nomination ON shadify_admin_nominations ((true)) WHERE state='pending';
CREATE FUNCTION protect_shadify_nomination() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Nomination history cannot be deleted'; END IF;
 IF OLD.state<>'pending' THEN RAISE EXCEPTION 'Terminal nomination cannot be changed'; END IF;
 IF NEW.id<>OLD.id OR NEW.app_instance_id<>OLD.app_instance_id OR NEW.deployment<>OLD.deployment
    OR NEW.revision<>OLD.revision+1
 THEN RAISE EXCEPTION 'Nomination scope/id/revision cannot be rewritten'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER shadify_nomination_history BEFORE UPDATE OR DELETE ON shadify_admin_nominations
 FOR EACH ROW EXECUTE FUNCTION protect_shadify_nomination();
CREATE UNIQUE INDEX shadify_nomination_operation_once ON shadify_app_admin_audit ((details->>'nomination_operation_id'))
 WHERE action LIKE 'nomination_%' AND details ? 'nomination_operation_id';
COMMIT;
