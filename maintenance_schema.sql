CREATE TABLE IF NOT EXISTS collector_quarantine_reports (
    site_id text PRIMARY KEY REFERENCES sites(id),
    customer_id text NOT NULL REFERENCES customers(id),
    instance_id text NOT NULL,
    total integer NOT NULL,
    complete boolean NOT NULL,
    reported_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS collector_quarantine_records (
    id uuid PRIMARY KEY,
    customer_id text NOT NULL REFERENCES customers(id),
    site_id text NOT NULL REFERENCES sites(id),
    instance_id text NOT NULL,
    record_id text NOT NULL,
    device_id text NOT NULL,
    cleanroom_id text NOT NULL,
    raw_text text,
    raw_sha256 text,
    metadata jsonb NOT NULL,
    validation jsonb,
    present boolean NOT NULL DEFAULT true,
    reported_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(customer_id,site_id,instance_id,record_id)
);
CREATE INDEX IF NOT EXISTS idx_collector_quarantine_scope ON collector_quarantine_records(customer_id,site_id,instance_id,present,record_id);
CREATE TABLE IF NOT EXISTS collector_quarantine_tasks (
    id uuid PRIMARY KEY,
    customer_id text NOT NULL REFERENCES customers(id),
    site_id text NOT NULL REFERENCES sites(id),
    instance_id text NOT NULL,
    record_entry_id uuid NOT NULL REFERENCES collector_quarantine_records(id) ON DELETE CASCADE,
    record_uuid text NOT NULL,
    raw_sha256 text NOT NULL,
    request_id uuid NOT NULL,
    actor_user_id uuid NOT NULL REFERENCES customer_users(id),
    state text NOT NULL CHECK(state IN ('pending','accepted','completed','expired','failed')),
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    accepted_at timestamptz,
    completed_at timestamptz,
    error text,
    failed_at timestamptz,
    UNIQUE(customer_id,site_id,request_id)
);
ALTER TABLE collector_quarantine_tasks ADD COLUMN IF NOT EXISTS error text;
ALTER TABLE collector_quarantine_tasks ADD COLUMN IF NOT EXISTS failed_at timestamptz;
ALTER TABLE collector_quarantine_tasks DROP CONSTRAINT IF EXISTS collector_quarantine_tasks_state_check;
ALTER TABLE collector_quarantine_tasks ADD CONSTRAINT collector_quarantine_tasks_state_check CHECK(state IN ('pending','accepted','completed','expired','failed'));
CREATE UNIQUE INDEX IF NOT EXISTS idx_collector_quarantine_one_task ON collector_quarantine_tasks(customer_id,site_id) WHERE state IN ('pending','accepted');
