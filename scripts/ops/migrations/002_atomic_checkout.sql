-- Migration 002: Atomic task checkout to prevent duplicate work across VMs
-- Issue: #348
--
-- Adds checkout fields and RPCs for atomic ticket claiming with expiry.

-- Add atomic checkout fields to swe_tickets
ALTER TABLE swe_tickets ADD COLUMN IF NOT EXISTS checkout_run_id UUID;
ALTER TABLE swe_tickets ADD COLUMN IF NOT EXISTS checkout_locked_at TIMESTAMPTZ;
ALTER TABLE swe_tickets ADD COLUMN IF NOT EXISTS checkout_locked_by TEXT;
ALTER TABLE swe_tickets ADD COLUMN IF NOT EXISTS checkout_expires_at TIMESTAMPTZ;

-- Index for finding unlocked tickets efficiently
CREATE INDEX IF NOT EXISTS idx_tickets_checkout
    ON swe_tickets (status, checkout_run_id)
    WHERE checkout_run_id IS NULL;

-- Atomic checkout RPC: claim a ticket or fail (returns true/false)
CREATE OR REPLACE FUNCTION atomic_checkout(
    p_ticket_id TEXT,
    p_run_id UUID,
    p_locked_by TEXT,
    p_lock_duration_minutes INT DEFAULT 60
) RETURNS BOOLEAN AS $$
DECLARE
    rows_affected INT;
BEGIN
    UPDATE swe_tickets
    SET checkout_run_id = p_run_id,
        checkout_locked_at = NOW(),
        checkout_locked_by = p_locked_by,
        checkout_expires_at = NOW() + (p_lock_duration_minutes || ' minutes')::INTERVAL,
        updated_at = NOW()
    WHERE ticket_id = p_ticket_id
      AND (checkout_run_id IS NULL OR checkout_expires_at < NOW())
      AND status NOT IN ('resolved', 'closed');

    GET DIAGNOSTICS rows_affected = ROW_COUNT;
    RETURN rows_affected > 0;
END;
$$ LANGUAGE plpgsql;

-- Release checkout RPC (only the holder can release)
CREATE OR REPLACE FUNCTION release_checkout(
    p_ticket_id TEXT,
    p_run_id UUID
) RETURNS BOOLEAN AS $$
DECLARE
    rows_affected INT;
BEGIN
    UPDATE swe_tickets
    SET checkout_run_id = NULL,
        checkout_locked_at = NULL,
        checkout_locked_by = NULL,
        checkout_expires_at = NULL,
        updated_at = NOW()
    WHERE ticket_id = p_ticket_id
      AND checkout_run_id = p_run_id;

    GET DIAGNOSTICS rows_affected = ROW_COUNT;
    RETURN rows_affected > 0;
END;
$$ LANGUAGE plpgsql;

-- Heartbeat RPC: extend lock duration
CREATE OR REPLACE FUNCTION checkout_heartbeat(
    p_ticket_id TEXT,
    p_run_id UUID,
    p_extend_minutes INT DEFAULT 60
) RETURNS BOOLEAN AS $$
DECLARE
    rows_affected INT;
BEGIN
    UPDATE swe_tickets
    SET checkout_expires_at = NOW() + (p_extend_minutes || ' minutes')::INTERVAL,
        updated_at = NOW()
    WHERE ticket_id = p_ticket_id
      AND checkout_run_id = p_run_id;

    GET DIAGNOSTICS rows_affected = ROW_COUNT;
    RETURN rows_affected > 0;
END;
$$ LANGUAGE plpgsql;

-- Force release RPC (admin override, ignores run_id)
CREATE OR REPLACE FUNCTION force_release_checkout(
    p_ticket_id TEXT
) RETURNS BOOLEAN AS $$
DECLARE
    rows_affected INT;
BEGIN
    UPDATE swe_tickets
    SET checkout_run_id = NULL,
        checkout_locked_at = NULL,
        checkout_locked_by = NULL,
        checkout_expires_at = NULL,
        updated_at = NOW()
    WHERE ticket_id = p_ticket_id
      AND checkout_run_id IS NOT NULL;

    GET DIAGNOSTICS rows_affected = ROW_COUNT;
    RETURN rows_affected > 0;
END;
$$ LANGUAGE plpgsql;

-- Cleanup expired locks RPC (returns count of released locks)
CREATE OR REPLACE FUNCTION cleanup_expired_checkouts()
RETURNS INT AS $$
DECLARE
    rows_affected INT;
BEGIN
    UPDATE swe_tickets
    SET checkout_run_id = NULL,
        checkout_locked_at = NULL,
        checkout_locked_by = NULL,
        checkout_expires_at = NULL,
        updated_at = NOW()
    WHERE checkout_run_id IS NOT NULL
      AND checkout_expires_at < NOW();

    GET DIAGNOSTICS rows_affected = ROW_COUNT;
    RETURN rows_affected;
END;
$$ LANGUAGE plpgsql;
