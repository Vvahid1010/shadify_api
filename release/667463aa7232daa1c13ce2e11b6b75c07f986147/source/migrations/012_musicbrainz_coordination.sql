-- Public metadata only. Applied locally solely to isolated test schemas.
CREATE TABLE shadify_mb_cache (
 request_key text PRIMARY KEY CHECK(request_key ~ '^[0-9a-f]{64}$'),
 request jsonb NOT NULL CHECK(octet_length(request::text)<=4096),
 response jsonb NOT NULL CHECK(octet_length(response::text)<=65536),
 fetched_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 expires_at timestamptz NOT NULL,
 response_status integer NOT NULL CHECK(response_status IN (200,404)),
 CHECK(expires_at>fetched_at AND expires_at<=fetched_at+interval '24 hours')
);
CREATE TABLE shadify_mb_queue (
 request_key text PRIMARY KEY CHECK(request_key ~ '^[0-9a-f]{64}$'),
 request jsonb NOT NULL CHECK(octet_length(request::text)<=4096),
 state text NOT NULL DEFAULT 'queued' CHECK(state IN ('queued','inflight','done','paused')),
 attempts integer NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 5),
 next_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 last_status integer CHECK(last_status BETWEEN 100 AND 599),
 last_error text CHECK(last_error IN ('provider_unavailable','retry_budget_exhausted','server_cooldown_review'))
);
CREATE INDEX shadify_mb_queue_due ON shadify_mb_queue(next_at,created_at) WHERE state IN ('queued','inflight');
CREATE TABLE shadify_mb_rate (
 singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
 next_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 cooldown_until timestamptz NOT NULL DEFAULT clock_timestamp(),
 inflight_until timestamptz,
 request_key text,
 attempt_id uuid,
 paused boolean NOT NULL DEFAULT false,
 CHECK((inflight_until IS NULL)=(attempt_id IS NULL)),
 CHECK((attempt_id IS NULL)=(request_key IS NULL))
);
INSERT INTO shadify_mb_rate(singleton) VALUES(true);
