CREATE TABLE IF NOT EXISTS customers (
    id text PRIMARY KEY,
    name text NOT NULL,
    enrollment_generation bigint NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE customers
    ADD COLUMN IF NOT EXISTS enrollment_generation bigint NOT NULL DEFAULT 1;

CREATE TABLE IF NOT EXISTS sites (
    id text PRIMARY KEY,
    customer_id text NOT NULL REFERENCES customers(id),
    name text NOT NULL,
    config_version bigint NOT NULL DEFAULT 1,
    collector_status jsonb NOT NULL DEFAULT '{}'::jsonb,
    last_heartbeat_at timestamptz,
    applied_config_version bigint,
    config_apply_status text NOT NULL DEFAULT 'awaiting',
    config_apply_error text,
    active_collector_instance text,
    active_collector_seen_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE sites ADD COLUMN IF NOT EXISTS retired_at timestamptz;

CREATE TABLE IF NOT EXISTS customer_users (
    id uuid PRIMARY KEY,
    customer_id text NOT NULL REFERENCES customers(id),
    username text NOT NULL UNIQUE,
    display_name text NOT NULL,
    password_salt text NOT NULL,
    password_hash text NOT NULL,
    role text NOT NULL DEFAULT 'customer_admin',
    enabled boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS customer_sessions (
    token_hash text PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES customer_users(id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_customer_users_username_lower
    ON customer_users(lower(username));
CREATE INDEX IF NOT EXISTS idx_customer_sessions_expiry ON customer_sessions(expires_at);
ALTER TABLE customer_users ALTER COLUMN role SET DEFAULT 'customer_admin';

CREATE TABLE IF NOT EXISTS cleanrooms (
    id text PRIMARY KEY,
    customer_id text NOT NULL REFERENCES customers(id),
    name text NOT NULL,
    sort_order integer NOT NULL DEFAULT 0,
    UNIQUE(customer_id, name)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_cleanrooms_customer_name_lower
    ON cleanrooms(customer_id, lower(name));

CREATE TABLE IF NOT EXISTS devices (
    id text PRIMARY KEY,
    customer_id text NOT NULL REFERENCES customers(id),
    cleanroom_id text NOT NULL REFERENCES cleanrooms(id),
    site_id text REFERENCES sites(id),
    name text NOT NULL,
    host text NOT NULL,
    tcp_port integer NOT NULL DEFAULT 502,
    slave integer NOT NULL DEFAULT 1,
    enabled boolean NOT NULL DEFAULT true,
    disabled_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    sort_order integer NOT NULL DEFAULT 0,
    CONSTRAINT devices_tcp_port_range CHECK (tcp_port BETWEEN 1 AND 65535),
    CONSTRAINT devices_slave_range CHECK (slave BETWEEN 1 AND 247)
);

ALTER TABLE sites ADD COLUMN IF NOT EXISTS config_version bigint NOT NULL DEFAULT 1;
ALTER TABLE sites ADD COLUMN IF NOT EXISTS collector_status jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE sites ADD COLUMN IF NOT EXISTS last_heartbeat_at timestamptz;
ALTER TABLE sites ADD COLUMN IF NOT EXISTS applied_config_version bigint;
ALTER TABLE sites ADD COLUMN IF NOT EXISTS config_apply_status text NOT NULL DEFAULT 'awaiting';
ALTER TABLE sites ADD COLUMN IF NOT EXISTS config_apply_error text;
ALTER TABLE sites ADD COLUMN IF NOT EXISTS active_collector_instance text;
ALTER TABLE sites ADD COLUMN IF NOT EXISTS active_collector_seen_at timestamptz;
ALTER TABLE sites ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();
CREATE UNIQUE INDEX IF NOT EXISTS idx_sites_customer_name_lower
    ON sites(customer_id, lower(name));
CREATE INDEX IF NOT EXISTS idx_sites_customer_heartbeat
    ON sites(customer_id, last_heartbeat_at DESC);
ALTER TABLE devices ADD COLUMN IF NOT EXISTS site_id text REFERENCES sites(id);
ALTER TABLE devices ADD COLUMN IF NOT EXISTS host text NOT NULL DEFAULT '192.168.2.83';
ALTER TABLE devices ALTER COLUMN host DROP DEFAULT;
ALTER TABLE devices ADD COLUMN IF NOT EXISTS tcp_port integer NOT NULL DEFAULT 502;
ALTER TABLE devices ADD COLUMN IF NOT EXISTS slave integer NOT NULL DEFAULT 1;
ALTER TABLE devices ADD COLUMN IF NOT EXISTS disabled_at timestamptz;
ALTER TABLE devices ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE devices DROP CONSTRAINT IF EXISTS devices_cleanroom_id_name_key;
UPDATE devices AS device
SET site_id = (
    SELECT site.id FROM sites AS site
    WHERE site.customer_id = device.customer_id
    ORDER BY site.created_at, site.id LIMIT 1
)
WHERE device.site_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_devices_active_room_name
    ON devices(cleanroom_id, lower(name)) WHERE enabled = true;
CREATE INDEX IF NOT EXISTS idx_devices_site_enabled
    ON devices(site_id, enabled, sort_order);
CREATE UNIQUE INDEX IF NOT EXISTS idx_devices_active_site_endpoint
    ON devices(customer_id, site_id, host, tcp_port, slave) WHERE enabled = true;

CREATE TABLE IF NOT EXISTS configuration_audit_events (
    id uuid PRIMARY KEY,
    customer_id text NOT NULL REFERENCES customers(id),
    actor_user_id uuid REFERENCES customer_users(id),
    actor_kind text NOT NULL,
    action text NOT NULL,
    target_type text NOT NULL,
    target_id text NOT NULL,
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT configuration_audit_actor_kind CHECK (actor_kind IN ('customer_user','edge'))
);
CREATE INDEX IF NOT EXISTS idx_configuration_audit_customer_time
    ON configuration_audit_events(customer_id, created_at DESC);

CREATE TABLE IF NOT EXISTS thresholds (
    cleanroom_id text PRIMARY KEY REFERENCES cleanrooms(id),
    customer_id text NOT NULL REFERENCES customers(id),
    profile_name text NOT NULL,
    particle_0_3_max double precision,
    particle_0_3_enabled boolean NOT NULL DEFAULT false,
    particle_0_5_max double precision NOT NULL DEFAULT 100000,
    particle_0_5_enabled boolean NOT NULL DEFAULT true,
    particle_1_0_max double precision,
    particle_1_0_enabled boolean NOT NULL DEFAULT false,
    particle_2_5_max double precision,
    particle_2_5_enabled boolean NOT NULL DEFAULT false,
    particle_5_0_max double precision,
    particle_5_0_enabled boolean NOT NULL DEFAULT false,
    particle_10_0_max double precision,
    particle_10_0_enabled boolean NOT NULL DEFAULT false,
    particle_5_max double precision NOT NULL DEFAULT 100000,
    temperature_min double precision NOT NULL,
    temperature_max double precision NOT NULL,
    humidity_min double precision NOT NULL,
    humidity_max double precision NOT NULL,
    alarm_delay_seconds integer NOT NULL DEFAULT 300
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'thresholds'
          AND column_name = 'particle_0_5_max'
    ) THEN
        ALTER TABLE thresholds
            ADD COLUMN particle_0_5_max double precision NOT NULL DEFAULT 100000;
        UPDATE thresholds SET particle_0_5_max = particle_5_max;
    END IF;
END $$;

ALTER TABLE thresholds ADD COLUMN IF NOT EXISTS particle_0_3_max double precision;
ALTER TABLE thresholds ADD COLUMN IF NOT EXISTS particle_0_3_enabled boolean NOT NULL DEFAULT false;
ALTER TABLE thresholds ADD COLUMN IF NOT EXISTS particle_0_5_enabled boolean NOT NULL DEFAULT true;
ALTER TABLE thresholds ADD COLUMN IF NOT EXISTS particle_1_0_max double precision;
ALTER TABLE thresholds ADD COLUMN IF NOT EXISTS particle_1_0_enabled boolean NOT NULL DEFAULT false;
ALTER TABLE thresholds ADD COLUMN IF NOT EXISTS particle_2_5_max double precision;
ALTER TABLE thresholds ADD COLUMN IF NOT EXISTS particle_2_5_enabled boolean NOT NULL DEFAULT false;
ALTER TABLE thresholds ADD COLUMN IF NOT EXISTS particle_5_0_max double precision;
ALTER TABLE thresholds ADD COLUMN IF NOT EXISTS particle_5_0_enabled boolean NOT NULL DEFAULT false;
ALTER TABLE thresholds ADD COLUMN IF NOT EXISTS particle_10_0_max double precision;
ALTER TABLE thresholds ADD COLUMN IF NOT EXISTS particle_10_0_enabled boolean NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS edge_tokens (
    token_hash text PRIMARY KEY,
    customer_id text NOT NULL REFERENCES customers(id),
    site_id text NOT NULL REFERENCES sites(id),
    label text NOT NULL,
    enabled boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    last_used_at timestamptz
);

CREATE TABLE IF NOT EXISTS collector_activation_tokens (
    token_hash text PRIMARY KEY,
    customer_id text NOT NULL REFERENCES customers(id),
    site_id text NOT NULL REFERENCES sites(id),
    created_by uuid NOT NULL REFERENCES customer_users(id),
    expires_at timestamptz NOT NULL,
    used_at timestamptz,
    claimed_machine_id text,
    created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE collector_activation_tokens
    ADD COLUMN IF NOT EXISTS claimed_machine_id text;
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname='collector_activation_machine_id'
          AND conrelid='collector_activation_tokens'::regclass
    ) THEN
        ALTER TABLE collector_activation_tokens
            ADD CONSTRAINT collector_activation_machine_id
            CHECK (claimed_machine_id IS NULL OR claimed_machine_id ~ '^[0-9a-f]{32}$')
            NOT VALID;
        ALTER TABLE collector_activation_tokens
            VALIDATE CONSTRAINT collector_activation_machine_id;
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS idx_collector_activation_site
    ON collector_activation_tokens(customer_id, site_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_collector_activation_expiry
    ON collector_activation_tokens(expires_at) WHERE used_at IS NULL;

CREATE TABLE IF NOT EXISTS collector_enrollments (
    id uuid PRIMARY KEY,
    customer_id text NOT NULL REFERENCES customers(id),
    machine_id text NOT NULL,
    enrollment_generation bigint NOT NULL DEFAULT 1,
    claim_secret_hash text NOT NULL,
    pairing_code text NOT NULL,
    hostname text NOT NULL,
    platform text NOT NULL,
    status text NOT NULL DEFAULT 'pending',
    site_id text UNIQUE REFERENCES sites(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    approved_at timestamptz,
    approved_by uuid REFERENCES customer_users(id),
    CONSTRAINT collector_enrollment_machine_id
        CHECK (machine_id ~ '^[0-9a-f]{32}$'),
    CONSTRAINT collector_enrollment_pairing_code
        CHECK (pairing_code ~ '^[A-F0-9]{8}$'),
    CONSTRAINT collector_enrollment_status
        CHECK (status IN ('pending','approved','revoked')),
    UNIQUE(customer_id, machine_id)
);
ALTER TABLE collector_enrollments
    ADD COLUMN IF NOT EXISTS enrollment_generation bigint NOT NULL DEFAULT 1;
CREATE UNIQUE INDEX IF NOT EXISTS idx_collector_enrollment_pending_code
    ON collector_enrollments(customer_id, pairing_code) WHERE status='pending';
CREATE INDEX IF NOT EXISTS idx_collector_enrollment_customer_status
    ON collector_enrollments(customer_id, status, last_seen_at DESC);

CREATE TABLE IF NOT EXISTS device_discovery_jobs (
    id uuid PRIMARY KEY,
    customer_id text NOT NULL REFERENCES customers(id),
    site_id text NOT NULL REFERENCES sites(id),
    cleanroom_id text REFERENCES cleanrooms(id),
    created_by uuid REFERENCES customer_users(id),
    requested_cidr text,
    scanned_cidr text,
    scanned_cidrs jsonb NOT NULL DEFAULT '[]'::jsonb,
    tcp_port integer NOT NULL DEFAULT 502,
    source text NOT NULL DEFAULT 'manual',
    status text NOT NULL DEFAULT 'pending',
    requested_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    completed_at timestamptz,
    result_count integer NOT NULL DEFAULT 0,
    error text,
    CONSTRAINT device_discovery_port_range CHECK (tcp_port BETWEEN 1 AND 65535),
    CONSTRAINT device_discovery_status CHECK (status IN ('pending','running','completed','failed')),
    CONSTRAINT device_discovery_source CHECK (source IN ('manual','automatic'))
);
ALTER TABLE device_discovery_jobs ALTER COLUMN cleanroom_id DROP NOT NULL;
ALTER TABLE device_discovery_jobs ALTER COLUMN created_by DROP NOT NULL;
ALTER TABLE device_discovery_jobs ADD COLUMN IF NOT EXISTS scanned_cidrs jsonb NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE device_discovery_jobs ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT 'manual';
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname='device_discovery_source'
          AND conrelid='device_discovery_jobs'::regclass
    ) THEN
        ALTER TABLE device_discovery_jobs
            ADD CONSTRAINT device_discovery_source
            CHECK (source IN ('manual','automatic')) NOT VALID;
        ALTER TABLE device_discovery_jobs VALIDATE CONSTRAINT device_discovery_source;
    END IF;
END $$;
WITH ranked_active_jobs AS (
    SELECT id,row_number() OVER (
        PARTITION BY site_id ORDER BY requested_at DESC,id
    ) AS position
    FROM device_discovery_jobs
    WHERE status IN ('pending','running')
)
UPDATE device_discovery_jobs AS job
SET status='failed',completed_at=now(),error='Superseded duplicate active scan during migration'
FROM ranked_active_jobs AS ranked
WHERE job.id=ranked.id AND ranked.position > 1;
DO $$ BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_class AS index_class
        JOIN pg_index AS index_definition ON index_definition.indexrelid=index_class.oid
        WHERE index_class.relname='idx_device_discovery_one_active_site'
          AND index_definition.indisunique=false
    ) THEN
        DROP INDEX idx_device_discovery_one_active_site;
    END IF;
END $$;
CREATE UNIQUE INDEX IF NOT EXISTS idx_device_discovery_one_active_site
    ON device_discovery_jobs(site_id) WHERE status IN ('pending','running');
CREATE INDEX IF NOT EXISTS idx_device_discovery_customer_time
    ON device_discovery_jobs(customer_id, requested_at DESC);

CREATE TABLE IF NOT EXISTS device_discovery_results (
    job_id uuid NOT NULL REFERENCES device_discovery_jobs(id) ON DELETE CASCADE,
    host text NOT NULL,
    tcp_port integer NOT NULL,
    slave integer NOT NULL DEFAULT 1,
    verified boolean NOT NULL DEFAULT false,
    modbus_responded boolean NOT NULL DEFAULT false,
    protocol_compatible boolean NOT NULL DEFAULT false,
    identity_verified boolean NOT NULL DEFAULT false,
    firmware_raw integer,
    particle_unit_code integer,
    particle_unit_label text,
    unit_supported boolean NOT NULL DEFAULT false,
    latency_ms integer,
    discovered_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(job_id, host, tcp_port),
    CONSTRAINT device_discovery_result_port_range CHECK (tcp_port BETWEEN 1 AND 65535),
    CONSTRAINT device_discovery_result_slave_range CHECK (slave BETWEEN 1 AND 247)
);
ALTER TABLE device_discovery_results ADD COLUMN IF NOT EXISTS modbus_responded boolean NOT NULL DEFAULT false;
ALTER TABLE device_discovery_results ADD COLUMN IF NOT EXISTS protocol_compatible boolean NOT NULL DEFAULT false;
ALTER TABLE device_discovery_results ADD COLUMN IF NOT EXISTS identity_verified boolean NOT NULL DEFAULT false;
ALTER TABLE device_discovery_results ADD COLUMN IF NOT EXISTS firmware_raw integer;
ALTER TABLE device_discovery_results ADD COLUMN IF NOT EXISTS particle_unit_code integer;
ALTER TABLE device_discovery_results ADD COLUMN IF NOT EXISTS particle_unit_label text;
ALTER TABLE device_discovery_results ADD COLUMN IF NOT EXISTS unit_supported boolean NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS readings (
    record_uuid uuid PRIMARY KEY,
    customer_id text NOT NULL REFERENCES customers(id),
    site_id text NOT NULL REFERENCES sites(id),
    cleanroom_id text NOT NULL,
    cleanroom_name text NOT NULL,
    device_id text NOT NULL,
    device_name text NOT NULL,
    measured_at timestamptz NOT NULL,
    received_at timestamptz NOT NULL DEFAULT now(),
    source text NOT NULL,
    particles jsonb NOT NULL,
    environment jsonb NOT NULL,
    alarm_status text NOT NULL,
    alarm_details jsonb NOT NULL DEFAULT '[]'::jsonb,
    cleanliness_code integer,
    cleanliness_label text,
    particle_unit_code integer,
    particle_unit_label text,
    protocol_profile text
);
ALTER TABLE readings ADD COLUMN IF NOT EXISTS particle_unit_code integer;
ALTER TABLE readings ADD COLUMN IF NOT EXISTS particle_unit_label text;
ALTER TABLE readings ADD COLUMN IF NOT EXISTS protocol_profile text;

CREATE INDEX IF NOT EXISTS idx_readings_customer_time
    ON readings(customer_id, measured_at DESC);
CREATE INDEX IF NOT EXISTS idx_readings_device_time
    ON readings(customer_id, device_id, measured_at DESC);
CREATE INDEX IF NOT EXISTS idx_readings_room_time
    ON readings(customer_id, cleanroom_id, measured_at DESC);
CREATE INDEX IF NOT EXISTS idx_readings_alarm_time
    ON readings(customer_id, alarm_status, measured_at DESC);

CREATE TABLE IF NOT EXISTS latest_readings (
    customer_id text NOT NULL REFERENCES customers(id),
    site_id text NOT NULL REFERENCES sites(id),
    cleanroom_id text NOT NULL,
    cleanroom_name text NOT NULL,
    device_id text NOT NULL,
    device_name text NOT NULL,
    measured_at timestamptz NOT NULL,
    received_at timestamptz NOT NULL DEFAULT now(),
    source text NOT NULL,
    particles jsonb NOT NULL,
    environment jsonb NOT NULL,
    alarm_status text NOT NULL,
    alarm_details jsonb NOT NULL DEFAULT '[]'::jsonb,
    cleanliness_code integer,
    cleanliness_label text,
    particle_unit_code integer,
    particle_unit_label text,
    protocol_profile text,
    PRIMARY KEY(customer_id, site_id, device_id)
);
ALTER TABLE latest_readings ADD COLUMN IF NOT EXISTS particle_unit_code integer;
ALTER TABLE latest_readings ADD COLUMN IF NOT EXISTS particle_unit_label text;
ALTER TABLE latest_readings ADD COLUMN IF NOT EXISTS protocol_profile text;
CREATE INDEX IF NOT EXISTS idx_latest_readings_room
    ON latest_readings(customer_id, cleanroom_id);

CREATE TABLE IF NOT EXISTS alarm_events (
    event_uuid uuid PRIMARY KEY,
    customer_id text NOT NULL REFERENCES customers(id),
    site_id text NOT NULL REFERENCES sites(id),
    cleanroom_id text NOT NULL,
    device_id text NOT NULL,
    source text NOT NULL CHECK (source = 'device'),
    metric text NOT NULL,
    started_at timestamptz NOT NULL,
    ended_at timestamptz,
    trigger_value double precision,
    peak_value double precision,
    limit_description text NOT NULL,
    received_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE alarm_events ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT 'device';
CREATE INDEX IF NOT EXISTS idx_alarm_events_customer_time
    ON alarm_events(customer_id, started_at DESC);
UPDATE alarm_events SET ended_at = COALESCE(ended_at, now())
WHERE metric = 'particle_5_um';

CREATE TABLE IF NOT EXISTS email_alert_settings (
    customer_id text PRIMARY KEY REFERENCES customers(id),
    enabled boolean NOT NULL DEFAULT false,
    recipients jsonb NOT NULL DEFAULT '[]'::jsonb,
    notify_recovery boolean NOT NULL DEFAULT true,
    enabled_since timestamptz NOT NULL DEFAULT now(),
    last_test_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS email_notifications (
    id uuid PRIMARY KEY,
    customer_id text NOT NULL REFERENCES customers(id),
    event_uuid uuid REFERENCES alarm_events(event_uuid),
    kind text NOT NULL CHECK (kind IN ('alarm','recovery','test')),
    recipient text NOT NULL,
    payload jsonb NOT NULL,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','sent','failed','cancelled')),
    attempts integer NOT NULL DEFAULT 0,
    last_error text,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    sent_at timestamptz,
    UNIQUE(customer_id,event_uuid,kind,recipient)
);
CREATE INDEX IF NOT EXISTS idx_email_notifications_pending
    ON email_notifications(next_attempt_at,created_at) WHERE status='pending';
CREATE INDEX IF NOT EXISTS idx_email_notifications_customer
    ON email_notifications(customer_id,created_at DESC);
ALTER TABLE email_notifications ADD COLUMN IF NOT EXISTS claim_token uuid;
ALTER TABLE email_notifications ADD COLUMN IF NOT EXISTS lease_until timestamptz;
ALTER TABLE email_notifications DROP CONSTRAINT IF EXISTS email_notifications_status_check;
ALTER TABLE email_notifications ADD CONSTRAINT email_notifications_status_check
    CHECK (status IN ('pending','sending','sent','failed','cancelled'));
CREATE INDEX IF NOT EXISTS idx_email_notifications_lease
    ON email_notifications(lease_until) WHERE status='sending';
CREATE TABLE IF NOT EXISTS email_worker_status (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    heartbeat_at timestamptz NOT NULL,
    smtp_configured boolean NOT NULL
);

-- Device lifecycle: retain assignment intervals so moves and restores never
-- authorize uploads from a retirement gap or rewrite historical ownership.
CREATE TABLE IF NOT EXISTS device_assignment_periods (
    id bigserial PRIMARY KEY,
    customer_id text NOT NULL REFERENCES customers(id),
    device_id text NOT NULL REFERENCES devices(id),
    site_id text REFERENCES sites(id),
    cleanroom_id text NOT NULL REFERENCES cleanrooms(id),
    device_name text NOT NULL,
    cleanroom_name text NOT NULL,
    started_at timestamptz NOT NULL,
    ended_at timestamptz
);
CREATE INDEX IF NOT EXISTS idx_device_assignment_scope
    ON device_assignment_periods(customer_id,device_id,site_id,cleanroom_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_device_assignment_current
    ON device_assignment_periods(device_id) WHERE ended_at IS NULL;
INSERT INTO device_assignment_periods(customer_id,device_id,site_id,cleanroom_id,device_name,cleanroom_name,started_at,ended_at)
SELECT d.customer_id,d.id,d.site_id,d.cleanroom_id,d.name,r.name,'-infinity'::timestamptz,
       CASE WHEN d.enabled THEN NULL ELSE COALESCE(d.disabled_at,now()) END
FROM devices d JOIN cleanrooms r ON r.id=d.cleanroom_id
WHERE NOT EXISTS (SELECT 1 FROM device_assignment_periods p WHERE p.device_id=d.id);

CREATE OR REPLACE FUNCTION track_device_assignment() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE change_time timestamptz := clock_timestamp();
BEGIN
    IF TG_OP='UPDATE' THEN
        IF (OLD.enabled,OLD.site_id,OLD.cleanroom_id,OLD.name,OLD.host,OLD.tcp_port,OLD.slave)
           IS NOT DISTINCT FROM
           (NEW.enabled,NEW.site_id,NEW.cleanroom_id,NEW.name,NEW.host,NEW.tcp_port,NEW.slave) THEN
            RETURN NEW;
        END IF;
        IF NOT NEW.enabled THEN change_time := COALESCE(NEW.disabled_at,change_time); END IF;
        UPDATE device_assignment_periods SET ended_at=change_time
        WHERE device_id=NEW.id AND ended_at IS NULL;
        DELETE FROM latest_readings WHERE customer_id=NEW.customer_id AND device_id=NEW.id;
        IF NOT NEW.enabled THEN
            UPDATE device_maintenance_periods SET ended_at=change_time
            WHERE device_id=NEW.id AND ended_at IS NULL AND ends_at>change_time;
        END IF;
    END IF;
    IF NEW.enabled THEN
        INSERT INTO device_assignment_periods(customer_id,device_id,site_id,cleanroom_id,device_name,cleanroom_name,started_at)
        VALUES(NEW.customer_id,NEW.id,NEW.site_id,NEW.cleanroom_id,NEW.name,
               (SELECT name FROM cleanrooms WHERE id=NEW.cleanroom_id),
               CASE WHEN TG_OP='INSERT' THEN '-infinity'::timestamptz ELSE change_time END);
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_device_assignment ON devices;
CREATE TRIGGER trg_device_assignment AFTER INSERT OR UPDATE ON devices
    FOR EACH ROW EXECUTE FUNCTION track_device_assignment();

CREATE TABLE IF NOT EXISTS device_maintenance_periods (
    id uuid PRIMARY KEY,
    customer_id text NOT NULL REFERENCES customers(id),
    device_id text NOT NULL REFERENCES devices(id),
    reason text NOT NULL,
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    ends_at timestamptz NOT NULL,
    ended_at timestamptz,
    actor_user_id uuid NOT NULL REFERENCES customer_users(id),
    CHECK (ends_at > started_at)
);
CREATE INDEX IF NOT EXISTS idx_device_maintenance_scope
    ON device_maintenance_periods(customer_id,device_id,started_at,ends_at);

-- Explicit operator-confirmed duplicate registrations; retain original records.
CREATE TABLE IF NOT EXISTS device_registration_links (
    duplicate_device_id text PRIMARY KEY REFERENCES devices(id),
    canonical_device_id text NOT NULL REFERENCES devices(id),
    customer_id text NOT NULL REFERENCES customers(id),
    actor_user_id uuid NOT NULL REFERENCES customer_users(id),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (duplicate_device_id <> canonical_device_id)
);
CREATE INDEX IF NOT EXISTS idx_device_registration_canonical
    ON device_registration_links(customer_id,canonical_device_id);

-- Operational diagnostics are separate from measurement and audit history.
CREATE TABLE IF NOT EXISTS collector_diagnostic_logs (
    id bigserial PRIMARY KEY,
    customer_id text NOT NULL REFERENCES customers(id),
    site_id text NOT NULL REFERENCES sites(id),
    record_uuid uuid NOT NULL,
    instance_id text NOT NULL,
    measured_at timestamptz NOT NULL,
    received_at timestamptz NOT NULL DEFAULT now(),
    level text NOT NULL,
    event text NOT NULL,
    message text NOT NULL,
    UNIQUE(customer_id,site_id,record_uuid)
);
CREATE INDEX IF NOT EXISTS idx_collector_diagnostics_scope
    ON collector_diagnostic_logs(customer_id,site_id,id DESC);
