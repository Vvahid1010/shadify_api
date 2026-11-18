-- Proposed next additive migration: 017_discover_editorial.sql.
-- Source review only. Runtime owner must verify installed migration inventory first.
-- No automatic startup migration, seeded editor grant or published content.
BEGIN;
CREATE TABLE shadify_discover_state (
 singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
 draft_revision bigint NOT NULL DEFAULT 0 CHECK(draft_revision>=0),
 draft_status text NOT NULL DEFAULT 'draft' CHECK(draft_status IN ('draft','pending','published')),
 draft jsonb NOT NULL DEFAULT '{"rails":[]}' CHECK(jsonb_typeof(draft)='object'),
 submitted_revision bigint,
 published_revision bigint NOT NULL DEFAULT 0 CHECK(published_revision>=0),
 published jsonb NOT NULL DEFAULT '{"rails":[]}' CHECK(jsonb_typeof(published)='object'),
 published_by uuid REFERENCES shadify_users(id),
 published_at timestamptz,
 editor_revision bigint NOT NULL DEFAULT 0 CHECK(editor_revision>=0),
 CHECK(submitted_revision IS NULL OR submitted_revision=draft_revision),
 CHECK((draft_status='pending')=(submitted_revision IS NOT NULL))
);
INSERT INTO shadify_discover_state(singleton) VALUES(true);
CREATE TABLE shadify_discover_editors (
 user_id uuid PRIMARY KEY REFERENCES shadify_users(id) ON DELETE RESTRICT,
 granted_by uuid NOT NULL REFERENCES shadify_users(id) ON DELETE RESTRICT,
 granted_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
COMMIT;
