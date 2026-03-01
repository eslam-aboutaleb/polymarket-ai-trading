-- Migration 007: Admin audit trail
-- =================================

BEGIN;

CREATE TABLE IF NOT EXISTS admin_audit_logs (
    id SERIAL PRIMARY KEY,
    wallet_address VARCHAR(42) NOT NULL,
    action VARCHAR(20) NOT NULL,
    source VARCHAR(50) NOT NULL DEFAULT 'env_sync',
    performed_at TIMESTAMP NOT NULL DEFAULT NOW(),
    previous_state BOOLEAN NOT NULL,
    new_state BOOLEAN NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_admin_audit_logs_wallet_address
    ON admin_audit_logs(wallet_address);

CREATE INDEX IF NOT EXISTS ix_admin_audit_logs_performed_at
    ON admin_audit_logs(performed_at DESC);

COMMIT;
