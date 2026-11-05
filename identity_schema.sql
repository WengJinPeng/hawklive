-- Explicitly reviewed bindings. No automatic first-observation enrollment.
CREATE TABLE IF NOT EXISTS device_identity_bindings (
    customer_id TEXT NOT NULL REFERENCES customers(id),
    site_id TEXT NOT NULL REFERENCES sites(id),
    device_id TEXT NOT NULL REFERENCES devices(id),
    expected_mac TEXT,
    required BOOLEAN NOT NULL DEFAULT TRUE,
    evidence TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(customer_id,site_id,device_id),
    UNIQUE(customer_id,site_id,expected_mac)
);
ALTER TABLE configuration_audit_events DROP CONSTRAINT IF EXISTS configuration_audit_actor_kind;
ALTER TABLE configuration_audit_events ADD CONSTRAINT configuration_audit_actor_kind
    CHECK (actor_kind IN ('customer_user','edge','operator'));
