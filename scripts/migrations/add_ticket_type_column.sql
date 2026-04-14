-- Migration: Add ticket_type column to swe_tickets
-- Date: 2026-03-19
-- Issue: PGRST204 — Could not find the 'ticket_type' column of 'swe_tickets'
--
-- The SWETicket datamodel gained a ticket_type field (TicketType enum) that
-- was never migrated to the live Supabase schema.  Every POST to /swe_tickets
-- that includes ticket_type fails with HTTP 400.
--
-- Valid values mirror src/swe_team/models.py TicketType enum:
--   bug, feature, enhancement, infrastructure, documentation,
--   question, security, regression, unknown

ALTER TABLE swe_tickets
    ADD COLUMN IF NOT EXISTS ticket_type TEXT NOT NULL DEFAULT 'unknown';

-- Also update the base schema DDL so new deployments include it.
-- (See scripts/ops/supabase_schema.sql — patched in the same commit.)
