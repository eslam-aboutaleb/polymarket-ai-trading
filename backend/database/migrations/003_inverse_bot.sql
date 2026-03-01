-- Migration 003: Inverse Position Bot
-- Adds global settings + per-position config + action audit tables.
-- =============================================================

BEGIN;

-- ─── 1. Extend user_settings with inverse-bot fields ─────────
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='user_settings' AND column_name='inverse_bot_enabled') THEN
        ALTER TABLE user_settings ADD COLUMN inverse_bot_enabled BOOLEAN NOT NULL DEFAULT FALSE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='user_settings' AND column_name='inverse_bot_default_size_mode') THEN
        ALTER TABLE user_settings ADD COLUMN inverse_bot_default_size_mode VARCHAR(20) NOT NULL DEFAULT 'full_notional';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='user_settings' AND column_name='inverse_bot_fixed_amount') THEN
        ALTER TABLE user_settings ADD COLUMN inverse_bot_fixed_amount DOUBLE PRECISION NOT NULL DEFAULT 50.0;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='user_settings' AND column_name='inverse_bot_confidence_threshold') THEN
        ALTER TABLE user_settings ADD COLUMN inverse_bot_confidence_threshold INTEGER NOT NULL DEFAULT 75;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='user_settings' AND column_name='inverse_bot_cooldown_minutes') THEN
        ALTER TABLE user_settings ADD COLUMN inverse_bot_cooldown_minutes INTEGER NOT NULL DEFAULT 30;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='user_settings' AND column_name='inverse_bot_max_reversals_per_day') THEN
        ALTER TABLE user_settings ADD COLUMN inverse_bot_max_reversals_per_day INTEGER NOT NULL DEFAULT 3;
    END IF;
END$$;

-- ─── 2. New table: inverse_bot_positions ─────────────────────
CREATE TABLE IF NOT EXISTS inverse_bot_positions (
    id                   SERIAL PRIMARY KEY,
    user_id              INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_id             VARCHAR(200) NOT NULL,
    condition_id         VARCHAR(200) NOT NULL,
    market_title         VARCHAR(500) DEFAULT '',
    outcome              VARCHAR(100) DEFAULT '',
    enabled              BOOLEAN NOT NULL DEFAULT TRUE,
    size_mode_override   VARCHAR(20) NOT NULL DEFAULT 'inherit',
    fixed_amount_override DOUBLE PRECISION,
    status               VARCHAR(30) NOT NULL DEFAULT 'active',
    last_signal          VARCHAR(30),
    last_confidence      DOUBLE PRECISION,
    last_reasoning       TEXT,
    last_web_summary     TEXT,
    last_x_summary       TEXT,
    last_error           TEXT,
    last_recommendation  VARCHAR(30),
    last_alt_outcome     VARCHAR(100),
    last_alt_token_id    VARCHAR(200),
    last_evaluated_at    TIMESTAMP,
    last_reversed_at     TIMESTAMP,
    reversals_today      INTEGER NOT NULL DEFAULT 0,
    reversals_day        DATE,
    persistence_count    INTEGER NOT NULL DEFAULT 0,
    created_at           TIMESTAMP NOT NULL DEFAULT now(),
    updated_at           TIMESTAMP NOT NULL DEFAULT now(),
    CONSTRAINT uq_inverse_bot_user_token UNIQUE (user_id, token_id)
);

CREATE INDEX IF NOT EXISTS ix_inverse_bot_positions_user_id ON inverse_bot_positions(user_id);
CREATE INDEX IF NOT EXISTS ix_inverse_bot_positions_enabled ON inverse_bot_positions(enabled);
CREATE INDEX IF NOT EXISTS ix_inverse_bot_positions_condition ON inverse_bot_positions(condition_id);
CREATE INDEX IF NOT EXISTS ix_inverse_bot_positions_token ON inverse_bot_positions(token_id);

-- ─── 3. New table: inverse_bot_actions ───────────────────────
CREATE TABLE IF NOT EXISTS inverse_bot_actions (
    id                    SERIAL PRIMARY KEY,
    inverse_bot_position_id INTEGER NOT NULL REFERENCES inverse_bot_positions(id) ON DELETE CASCADE,
    user_id               INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    condition_id          VARCHAR(200) NOT NULL,
    from_token_id         VARCHAR(200) NOT NULL,
    to_token_id           VARCHAR(200),
    from_outcome          VARCHAR(100),
    to_outcome            VARCHAR(100),
    sell_order_hash       VARCHAR(200),
    buy_order_hash        VARCHAR(200),
    sell_size             DOUBLE PRECISION,
    buy_notional          DOUBLE PRECISION,
    confidence            DOUBLE PRECISION,
    recommendation        VARCHAR(30),
    status                VARCHAR(30) NOT NULL DEFAULT 'failed',
    error                 TEXT,
    executed_at           TIMESTAMP,
    created_at            TIMESTAMP NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_inverse_bot_actions_user_id ON inverse_bot_actions(user_id);
CREATE INDEX IF NOT EXISTS ix_inverse_bot_actions_position_id ON inverse_bot_actions(inverse_bot_position_id);
CREATE INDEX IF NOT EXISTS ix_inverse_bot_actions_condition ON inverse_bot_actions(condition_id);
CREATE INDEX IF NOT EXISTS ix_inverse_bot_actions_status ON inverse_bot_actions(status);

COMMIT;
