-- Migration 005: Followed trader alias support
-- ============================================

BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_name='followed_traders' AND column_name='trader_alias'
    ) THEN
        ALTER TABLE followed_traders ADD COLUMN trader_alias VARCHAR(100);
    END IF;
END$$;

COMMIT;
