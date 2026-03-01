-- Migration 002: Stop-Loss Orders
-- Adds a table for persistent stop-loss orders monitored in real-time.
-- =============================================================

BEGIN;

CREATE TABLE IF NOT EXISTS stop_loss_orders (
    id              SERIAL PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_id        VARCHAR(200) NOT NULL,
    market_id       VARCHAR(200) NOT NULL DEFAULT '',
    market_title    VARCHAR(500) DEFAULT '',
    outcome         VARCHAR(50)  DEFAULT '',
    size            DOUBLE PRECISION NOT NULL,
    stop_price      DOUBLE PRECISION NOT NULL,
    status          VARCHAR(30)  NOT NULL DEFAULT 'active',
    order_hash      VARCHAR(200),
    executed_price  DOUBLE PRECISION,
    triggered_at    TIMESTAMP,
    created_at      TIMESTAMP NOT NULL DEFAULT now(),
    updated_at      TIMESTAMP NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_stop_loss_user_id ON stop_loss_orders(user_id);
CREATE INDEX IF NOT EXISTS ix_stop_loss_status  ON stop_loss_orders(status);
CREATE INDEX IF NOT EXISTS ix_stop_loss_token   ON stop_loss_orders(token_id);

COMMIT;
