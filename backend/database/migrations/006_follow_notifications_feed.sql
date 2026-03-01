-- Migration 006: Notification follows + feed + position state
-- ===========================================================

BEGIN;

CREATE TABLE IF NOT EXISTS notification_followed_traders (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    trader_wallet VARCHAR(42) NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    feed_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    email_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_notification_user_trader UNIQUE (user_id, trader_wallet)
);

CREATE INDEX IF NOT EXISTS ix_notification_followed_traders_user_id
    ON notification_followed_traders(user_id);
CREATE INDEX IF NOT EXISTS ix_notification_followed_traders_trader_wallet
    ON notification_followed_traders(trader_wallet);

CREATE TABLE IF NOT EXISTS trader_position_state (
    id SERIAL PRIMARY KEY,
    trader_wallet VARCHAR(42) NOT NULL,
    token_id VARCHAR(200) NOT NULL,
    market_id VARCHAR(200) NOT NULL DEFAULT '',
    net_size DOUBLE PRECISION NOT NULL DEFAULT 0,
    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_trader_wallet_token UNIQUE (trader_wallet, token_id)
);

CREATE INDEX IF NOT EXISTS ix_trader_position_state_trader_wallet
    ON trader_position_state(trader_wallet);
CREATE INDEX IF NOT EXISTS ix_trader_position_state_token_id
    ON trader_position_state(token_id);

CREATE TABLE IF NOT EXISTS notification_feed_events (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    trader_wallet VARCHAR(42) NOT NULL,
    event_type VARCHAR(20) NOT NULL,
    market_id VARCHAR(200) NOT NULL DEFAULT '',
    token_id VARCHAR(200) NOT NULL DEFAULT '',
    side VARCHAR(20) NOT NULL DEFAULT '',
    size DOUBLE PRECISION NOT NULL DEFAULT 0,
    price DOUBLE PRECISION NOT NULL DEFAULT 0,
    prev_net_size DOUBLE PRECISION NOT NULL DEFAULT 0,
    new_net_size DOUBLE PRECISION NOT NULL DEFAULT 0,
    source_trade_history_id INTEGER NULL REFERENCES trade_history(id) ON DELETE SET NULL,
    email_status VARCHAR(20) NOT NULL DEFAULT 'pending',
    email_error TEXT NULL,
    emailed_at TIMESTAMP NULL,
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_notification_feed_events_user_created
    ON notification_feed_events(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_notification_feed_events_user_wallet_created
    ON notification_feed_events(user_id, trader_wallet, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_notification_feed_events_source_trade_history_id
    ON notification_feed_events(source_trade_history_id);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_name='user_settings'
          AND column_name='follow_email_notifications_enabled'
    ) THEN
        ALTER TABLE user_settings
            ADD COLUMN follow_email_notifications_enabled BOOLEAN NOT NULL DEFAULT FALSE;
    END IF;
END$$;

COMMIT;
