-- Migration 008: New features - Take Profit, Multi-Layer Risk, Quality Scoring, Arbitrage
BEGIN;

-- Take-profit orders table
CREATE TABLE IF NOT EXISTS take_profit_orders (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_id VARCHAR(200) NOT NULL,
    market_id VARCHAR(200),
    market_title VARCHAR(500),
    outcome VARCHAR(50),
    size DOUBLE PRECISION NOT NULL,
    take_profit_price DOUBLE PRECISION NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'active',
    order_hash VARCHAR(200),
    executed_price DOUBLE PRECISION,
    triggered_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_tp_user_status ON take_profit_orders(user_id, status);

-- User settings: multi-layer risk columns
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='user_settings' AND column_name='monthly_loss_limit') THEN
        ALTER TABLE user_settings ADD COLUMN monthly_loss_limit DOUBLE PRECISION;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='user_settings' AND column_name='max_drawdown_pct') THEN
        ALTER TABLE user_settings ADD COLUMN max_drawdown_pct DOUBLE PRECISION DEFAULT 25.0;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='user_settings' AND column_name='total_loss_halt_pct') THEN
        ALTER TABLE user_settings ADD COLUMN total_loss_halt_pct DOUBLE PRECISION DEFAULT 40.0;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='user_settings' AND column_name='peak_capital') THEN
        ALTER TABLE user_settings ADD COLUMN peak_capital DOUBLE PRECISION;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='user_settings' AND column_name='initial_capital') THEN
        ALTER TABLE user_settings ADD COLUMN initial_capital DOUBLE PRECISION;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='user_settings' AND column_name='trading_halted') THEN
        ALTER TABLE user_settings ADD COLUMN trading_halted BOOLEAN DEFAULT FALSE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='user_settings' AND column_name='halt_reason') THEN
        ALTER TABLE user_settings ADD COLUMN halt_reason VARCHAR(500);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='user_settings' AND column_name='cooldown_until') THEN
        ALTER TABLE user_settings ADD COLUMN cooldown_until TIMESTAMP WITH TIME ZONE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='user_settings' AND column_name='dynamic_sizing_enabled') THEN
        ALTER TABLE user_settings ADD COLUMN dynamic_sizing_enabled BOOLEAN DEFAULT FALSE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='user_settings' AND column_name='consecutive_wins') THEN
        ALTER TABLE user_settings ADD COLUMN consecutive_wins INTEGER DEFAULT 0;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='user_settings' AND column_name='consecutive_losses') THEN
        ALTER TABLE user_settings ADD COLUMN consecutive_losses INTEGER DEFAULT 0;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='user_settings' AND column_name='simulation_mode') THEN
        ALTER TABLE user_settings ADD COLUMN simulation_mode BOOLEAN DEFAULT FALSE;
    END IF;
END$$;

-- Winners: quality scoring columns
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='winners' AND column_name='quality_score') THEN
        ALTER TABLE winners ADD COLUMN quality_score DOUBLE PRECISION;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='winners' AND column_name='consistency_score') THEN
        ALTER TABLE winners ADD COLUMN consistency_score DOUBLE PRECISION;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='winners' AND column_name='risk_adjusted_score') THEN
        ALTER TABLE winners ADD COLUMN risk_adjusted_score DOUBLE PRECISION;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='winners' AND column_name='activity_score') THEN
        ALTER TABLE winners ADD COLUMN activity_score DOUBLE PRECISION;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='winners' AND column_name='quality_tier') THEN
        ALTER TABLE winners ADD COLUMN quality_tier VARCHAR(20);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='winners' AND column_name='quality_updated_at') THEN
        ALTER TABLE winners ADD COLUMN quality_updated_at TIMESTAMP WITH TIME ZONE;
    END IF;
END$$;

COMMIT;
