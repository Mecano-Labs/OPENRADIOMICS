-- Migration 007: Add trusted_devices table for "Trust this device" MFA bypass
-- Allows users to skip MFA on recognized devices for 90 days

CREATE TABLE trusted_devices (
    id              SERIAL PRIMARY KEY,
    user_email      VARCHAR(255) NOT NULL,
    device_token    VARCHAR(64) UNIQUE NOT NULL,
    device_name     VARCHAR(255),
    user_agent      TEXT,
    ip_address      INET,
    trusted_at      TIMESTAMPTZ DEFAULT NOW(),
    last_used_at    TIMESTAMPTZ DEFAULT NOW(),
    expires_at      TIMESTAMPTZ NOT NULL,
    is_active       BOOLEAN DEFAULT TRUE
);

-- Index for fast token lookup
CREATE INDEX idx_trusted_devices_token ON trusted_devices(device_token);

-- Index for finding all devices for a user
CREATE INDEX idx_trusted_devices_email ON trusted_devices(user_email);

-- Index for cleanup of expired devices
CREATE INDEX idx_trusted_devices_expires ON trusted_devices(expires_at) WHERE is_active = TRUE;

-- Record migration
INSERT INTO schema_version (version, description)
VALUES (7, 'Add trusted_devices table for MFA device trust feature');
