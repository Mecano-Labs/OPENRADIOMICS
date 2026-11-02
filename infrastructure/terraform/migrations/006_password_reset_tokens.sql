-- Migration 006: Add password_reset_tokens table for link-based password reset
-- Run with: psql -h <rds-endpoint> -U postgres -d openradiomics -f 006_password_reset_tokens.sql

-- Create the table
CREATE TABLE IF NOT EXISTS password_reset_tokens (
    id              SERIAL PRIMARY KEY,
    email           VARCHAR(255) NOT NULL,
    token           VARCHAR(64) UNIQUE NOT NULL,
    expires_at      TIMESTAMPTZ NOT NULL,
    used_at         TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Create indexes
CREATE INDEX IF NOT EXISTS idx_password_reset_token ON password_reset_tokens(token);
CREATE INDEX IF NOT EXISTS idx_password_reset_email ON password_reset_tokens(email);
CREATE INDEX IF NOT EXISTS idx_password_reset_expires ON password_reset_tokens(expires_at);

-- Record schema version
INSERT INTO schema_version (version, description)
VALUES (6, 'Add password_reset_tokens table for link-based password reset')
ON CONFLICT (version) DO NOTHING;
