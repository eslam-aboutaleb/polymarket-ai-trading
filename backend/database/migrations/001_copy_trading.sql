-- Migration 001: Copy-Trading & Enhanced Leaderboard
-- Run against the "polymarket" PostgreSQL database
-- =============================================================

BEGIN;

-- ─── 1. New table: followed_traders ─────────────────────────
CREATE TABLE IF NOT EXISTS followed_traders (
    id          SERIAL PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    trader_wallet VARCHAR(42) NOT NULL,
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    max_position_size DOUBLE PRECISION,
    created_at  TIMESTAMP NOT NULL DEFAULT now(),
    updated_at  TIMESTAMP NOT NULL DEFAULT now(),
    CONSTRAINT uq_user_trader UNIQUE (user_id, trader_wallet)
);
CREATE INDEX IF NOT EXISTS ix_followed_traders_user_id ON followed_traders(user_id);
CREATE INDEX IF NOT EXISTS ix_followed_traders_trader_wallet ON followed_traders(trader_wallet);

-- ─── 2. Extend winners table ────────────────────────────────
-- Add columns only if they don't already exist (idempotent).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='winners' AND column_name='display_name') THEN
        ALTER TABLE winners ADD COLUMN display_name VARCHAR(200);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='winners' AND column_name='profile_image') THEN
        ALTER TABLE winners ADD COLUMN profile_image VARCHAR(500);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='winners' AND column_name='markets_traded') THEN
        ALTER TABLE winners ADD COLUMN markets_traded INTEGER DEFAULT 0;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='winners' AND column_name='pnl_24h') THEN
        ALTER TABLE winners ADD COLUMN pnl_24h DOUBLE PRECISION DEFAULT 0.0;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='winners' AND column_name='pnl_7d') THEN
        ALTER TABLE winners ADD COLUMN pnl_7d DOUBLE PRECISION DEFAULT 0.0;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='winners' AND column_name='pnl_30d') THEN
        ALTER TABLE winners ADD COLUMN pnl_30d DOUBLE PRECISION DEFAULT 0.0;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='winners' AND column_name='volume') THEN
        ALTER TABLE winners ADD COLUMN volume DOUBLE PRECISION DEFAULT 0.0;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='winners' AND column_name='volume_24h') THEN
        ALTER TABLE winners ADD COLUMN volume_24h DOUBLE PRECISION DEFAULT 0.0;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='winners' AND column_name='positions_value') THEN
        ALTER TABLE winners ADD COLUMN positions_value DOUBLE PRECISION DEFAULT 0.0;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='winners' AND column_name='leaderboard_rank') THEN
        ALTER TABLE winners ADD COLUMN leaderboard_rank INTEGER;
    END IF;
END$$;

-- ─── 3. Extend user_settings with copy-trading columns ──────
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='user_settings' AND column_name='copy_trading_enabled') THEN
        ALTER TABLE user_settings ADD COLUMN copy_trading_enabled BOOLEAN NOT NULL DEFAULT FALSE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='user_settings' AND column_name='risk_mode') THEN
        ALTER TABLE user_settings ADD COLUMN risk_mode VARCHAR(30) NOT NULL DEFAULT 'max_position_daily_loss';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='user_settings' AND column_name='max_position_size') THEN
        ALTER TABLE user_settings ADD COLUMN max_position_size DOUBLE PRECISION DEFAULT 100.0;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='user_settings' AND column_name='daily_loss_limit') THEN
        ALTER TABLE user_settings ADD COLUMN daily_loss_limit DOUBLE PRECISION DEFAULT 500.0;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='user_settings' AND column_name='mirror_percentage') THEN
        ALTER TABLE user_settings ADD COLUMN mirror_percentage DOUBLE PRECISION DEFAULT 10.0;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='user_settings' AND column_name='fixed_trade_amount') THEN
        ALTER TABLE user_settings ADD COLUMN fixed_trade_amount DOUBLE PRECISION DEFAULT 50.0;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='user_settings' AND column_name='require_ai_approval') THEN
        ALTER TABLE user_settings ADD COLUMN require_ai_approval BOOLEAN NOT NULL DEFAULT TRUE;
    END IF;
END$$;

COMMIT;
