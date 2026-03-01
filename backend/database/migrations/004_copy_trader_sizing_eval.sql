-- Migration 004: Per-trader copy sizing + evaluation metadata
-- =============================================================

BEGIN;

-- ─── 1. Extend followed_traders with sizing/allocation fields ───────────────
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='followed_traders' AND column_name='sizing_mode') THEN
        ALTER TABLE followed_traders ADD COLUMN sizing_mode VARCHAR(40) NOT NULL DEFAULT 'inherit_global';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='followed_traders' AND column_name='fixed_trade_amount_override') THEN
        ALTER TABLE followed_traders ADD COLUMN fixed_trade_amount_override DOUBLE PRECISION;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='followed_traders' AND column_name='copy_wallet_mode') THEN
        ALTER TABLE followed_traders ADD COLUMN copy_wallet_mode VARCHAR(50) NOT NULL DEFAULT 'dynamic_main_wallet_percentage';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='followed_traders' AND column_name='copy_wallet_percentage') THEN
        ALTER TABLE followed_traders ADD COLUMN copy_wallet_percentage DOUBLE PRECISION NOT NULL DEFAULT 100.0;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='followed_traders' AND column_name='copy_wallet_fixed_amount') THEN
        ALTER TABLE followed_traders ADD COLUMN copy_wallet_fixed_amount DOUBLE PRECISION;
    END IF;
END$$;

-- ─── 2. Extend trade_history with source metadata ────────────────────────────
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='trade_history' AND column_name='notional_usdc') THEN
        ALTER TABLE trade_history ADD COLUMN notional_usdc DOUBLE PRECISION;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='trade_history' AND column_name='source_trade_id_ext') THEN
        ALTER TABLE trade_history ADD COLUMN source_trade_id_ext VARCHAR(120);
    END IF;
END$$;

CREATE INDEX IF NOT EXISTS ix_trade_history_source_trade_id_ext ON trade_history(source_trade_id_ext);

-- ─── 3. Extend user_trades with copy evaluation metadata ─────────────────────
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='user_trades' AND column_name='source_trade_history_id') THEN
        ALTER TABLE user_trades ADD COLUMN source_trade_history_id INTEGER REFERENCES trade_history(id) ON DELETE SET NULL;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='user_trades' AND column_name='trader_trade_notional') THEN
        ALTER TABLE user_trades ADD COLUMN trader_trade_notional DOUBLE PRECISION;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='user_trades' AND column_name='trader_wallet_balance') THEN
        ALTER TABLE user_trades ADD COLUMN trader_wallet_balance DOUBLE PRECISION;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='user_trades' AND column_name='copy_wallet_base') THEN
        ALTER TABLE user_trades ADD COLUMN copy_wallet_base DOUBLE PRECISION;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='user_trades' AND column_name='sizing_mode_applied') THEN
        ALTER TABLE user_trades ADD COLUMN sizing_mode_applied VARCHAR(50);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='user_trades' AND column_name='copy_wallet_mode_applied') THEN
        ALTER TABLE user_trades ADD COLUMN copy_wallet_mode_applied VARCHAR(50);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='user_trades' AND column_name='calculation_warning') THEN
        ALTER TABLE user_trades ADD COLUMN calculation_warning TEXT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='user_trades' AND column_name='calculation_details') THEN
        ALTER TABLE user_trades ADD COLUMN calculation_details TEXT;
    END IF;
END$$;

CREATE INDEX IF NOT EXISTS ix_user_trades_source_trade_history_id ON user_trades(source_trade_history_id);

COMMIT;
